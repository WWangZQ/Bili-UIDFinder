using System.Collections.Concurrent;
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace BiliSearch.UI.Services;

// ---------------------------------------------------------------------------
// Proxy pool — fetches free proxies, validates, round-robins
// ---------------------------------------------------------------------------

public sealed class ProxyPool
{
    private readonly List<string> _proxies = [];
    private int _idx;
    private readonly object _lock = new();

    public int Alive { get { lock (_lock) return _proxies.Count; } }

    public void AddRange(IEnumerable<string> proxies)
    {
        lock (_lock) { _proxies.AddRange(proxies); }
    }

    public List<string> Snapshot()
    {
        lock (_lock) return [.. _proxies];
    }

    public void ReplaceWith(IEnumerable<string> proxies)
    {
        lock (_lock)
        {
            _proxies.Clear();
            _proxies.AddRange(proxies);
            _idx = 0;
        }
    }

    public string? Next()
    {
        lock (_lock)
        {
            if (_proxies.Count == 0) return null;
            var p = _proxies[_idx % _proxies.Count];
            _idx++;
            return p;
        }
    }

    public void Remove(string proxy)
    {
        lock (_lock) { _proxies.Remove(proxy); }
    }
}

public static class ProxyFetcher
{
    private static readonly HttpClient Client = new();

    public static async Task<List<string>> FetchAsync(CancellationToken ct = default)
    {
        var (geonode, pubproxy) = (
            FetchGeonodeAsync(ct),
            FetchPubProxyAsync(ct)
        );
        await Task.WhenAll(geonode, pubproxy);

        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var result = new List<string>();
        foreach (var p in geonode.Result.Concat(pubproxy.Result))
        {
            if (seen.Add(p)) result.Add(p);
        }
        return result;
    }

    private static async Task<List<string>> FetchGeonodeAsync(CancellationToken ct)
    {
        var proxies = new List<string>();
        for (int page = 1; page <= 3; page++)
        {
            try
            {
                var url = $"https://proxylist.geonode.com/api/proxy-list?limit=50&page={page}&sort_by=lastChecked&sort_type=desc&protocols=http";
                var resp = await Client.GetAsync(url, ct);
                if (!resp.IsSuccessStatusCode) break;
                using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(ct));
                foreach (var item in doc.RootElement.GetProperty("data").EnumerateArray())
                {
                    var ip = item.GetProperty("ip").GetString();
                    var portStr = item.GetProperty("port").GetString();
                    if (ip != null && portStr != null && int.TryParse(portStr, out _))
                        proxies.Add($"http://{ip}:{portStr}");
                }
            }
            catch { break; }
        }
        return proxies;
    }

    private static async Task<List<string>> FetchPubProxyAsync(CancellationToken ct)
    {
        var proxies = new List<string>();
        try
        {
            var resp = await Client.GetAsync("http://pubproxy.com/api/proxy?type=http&limit=20", ct);
            if (!resp.IsSuccessStatusCode) return proxies;
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(ct));
            foreach (var item in doc.RootElement.GetProperty("data").EnumerateArray())
            {
                var ip = item.GetProperty("ip").GetString();
                var portStr = item.GetProperty("port").GetString();
                if (ip != null && portStr != null && int.TryParse(portStr, out _))
                    proxies.Add($"http://{ip}:{portStr}");
            }
        }
        catch { }
        return proxies;
    }

    public static async Task ValidateAsync(ProxyPool pool, CancellationToken ct = default)
    {
        const int concurrentCheck = 20;
        using var sem = new SemaphoreSlim(concurrentCheck);

        var toCheck = pool.Snapshot();
        var valid = new ConcurrentBag<string>();
        var tasks = toCheck.Select(async proxy =>
        {
            await sem.WaitAsync(ct);
            try
            {
                using var handler = new HttpClientHandler { Proxy = new WebProxy(proxy), UseProxy = true };
                using var client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(10) };
                var resp = await client.GetAsync(
                    "https://api.bilibili.com/x/web-interface/card?mid=1&photo=false", ct);
                if (resp.IsSuccessStatusCode &&
                    resp.Content.Headers.ContentType?.MediaType?.Contains("json") == true)
                {
                    valid.Add(proxy);
                }
            }
            catch { }
            finally { sem.Release(); }
        });
        await Task.WhenAll(tasks);

        pool.ReplaceWith(valid);
    }
}

// ---------------------------------------------------------------------------
// Scan engine — pure C# port of gui.py ScanEngine
// ---------------------------------------------------------------------------

public sealed class ScanEngine
{
    private static readonly Regex EnglishRe = new(@"^[A-Za-z]+$", RegexOptions.Compiled);
    private const string ApiUrl = "https://api.bilibili.com/x/web-interface/card";
    private const int Concurrency = 5;
    private const int MaxRetries = 3;
    private const int RetryDelaySec = 3;

    private static readonly HttpClient DefaultClient = new()
    {
        DefaultRequestHeaders =
        {
            { "User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36" },
            { "Referer", "https://space.bilibili.com/" },
        }
    };

    // Events for the UI
    public event Action<string>? LogReceived;     // raw log line
    public event Action<int, int, int>? Progress; // done, total, foundCount
    public event Action<bool>? ScanFinished;      // true if completed normally

    public bool Running { get; private set; }
    public int Done { get; private set; }
    public int Total { get; private set; }
    public int FoundCount { get; private set; }
    public List<(string Uid, string Nickname, string Status)> Results { get; } = [];

    private CancellationTokenSource? _cts;

    // -- UID generators --

    public static List<string> GenerateSuffixUids(string suffix)
    {
        var sfx = int.Parse(suffix);
        var multiplier = (int)Math.Pow(10, suffix.Length); // 10000 for 4-digit suffix
        var uids = new List<string>(999);
        for (int p = 1; p <= 999; p++)
            uids.Add((p * multiplier + sfx).ToString());   // no leading zeros
        return uids;
    }

    public static List<string> GeneratePalindromes(int digits)
    {
        var uids = new List<string>();
        int half = digits / 2;
        int start = (int)Math.Pow(10, half + digits % 2 - 1);
        int end = (int)Math.Pow(10, half + digits % 2);
        for (int left = start; left < end; left++)
        {
            var s = left.ToString();
            string uid;
            if (digits % 2 == 0)
                uid = s + Reverse(s);
            else
                uid = s + Reverse(s[..^1]);
            uids.Add(uid);
        }
        return uids;
    }

    private static string Reverse(string s)
    {
        var arr = s.ToCharArray();
        Array.Reverse(arr);
        return new string(arr);
    }

    // -- Scanning --

    public async Task StartAsync(
        List<string> uids,
        string? fixedProxy,
        bool usePool,
        CancellationToken ct = default)
    {
        if (Running) return;
        Running = true;
        Done = 0;
        FoundCount = 0;
        Total = uids.Count;
        Results.Clear();
        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);

        Log($"开始扫描，共 {uids.Count} 个 UID");

        ProxyPool? pool = null;
        if (usePool)
        {
            Log("正在获取免费代理...");
            var proxies = await ProxyFetcher.FetchAsync(ct);
            pool = new ProxyPool();
            pool.AddRange(proxies);
            Log($"获取到 {proxies.Count} 个代理，正在验证...");
            await ProxyFetcher.ValidateAsync(pool, ct);
            Log($"验证通过 {pool.Alive} 个代理");
            if (pool.Alive == 0)
            {
                Log("没有可用代理，退出");
                Running = false;
                ScanFinished?.Invoke(false);
                return;
            }
        }

        var queue = new BlockingCollection<(int idx, string uid)>(new ConcurrentQueue<(int, string)>(), uids.Count + Concurrency);
        var workers = new Task[Concurrency];
        for (int w = 0; w < Concurrency; w++)
            workers[w] = Task.Run(() => WorkerAsync(queue, pool, fixedProxy, _cts.Token));

        for (int i = 0; i < uids.Count; i++)
        {
            if (_cts.Token.IsCancellationRequested) break;
            queue.Add((i + 1, uids[i]));
        }
        queue.CompleteAdding();

        await Task.WhenAll(workers);

        var found = Results.Count(r => r.Status == "found");
        Log($"扫描完成！共 {Results.Count} 个结果，{found} 个符合条件");
        Running = false;
        ScanFinished?.Invoke(true);
    }

    public void Stop()
    {
        _cts?.Cancel();
        Running = false;
    }

    private async Task WorkerAsync(
        BlockingCollection<(int idx, string uid)> queue,
        ProxyPool? pool,
        string? fixedProxy,
        CancellationToken ct)
    {
        var proxy = fixedProxy ?? pool?.Next();
        var client = CreateClient(proxy);

        try
        {
            foreach (var (idx, uid) in queue.GetConsumingEnumerable(ct))
            {
                if (ct.IsCancellationRequested) break;

                var (rUid, rNick, rStatus) = await CheckOneAsync(client, uid, idx, Total, ct);

                // Handle blocked → rotate proxy
                if (rStatus == "blocked" && pool != null)
                {
                    pool.Remove(proxy!);
                    client.Dispose();
                    proxy = pool.Next();
                    client = CreateClient(proxy);
                }

                if (rStatus != "cancelled")
                {
                    if (rStatus == "found") FoundCount++;
                    Results.Add((rUid, rNick, rStatus));

                    var tag = rStatus == "found" ? "FOUND" : rStatus;
                    Log($"[{idx}/{Total}][{tag}] {rUid} {rNick}".TrimEnd());
                }

                Done = idx;
                Progress?.Invoke(Done, Total, FoundCount);
            }
        }
        finally
        {
            client.Dispose();
        }
    }

    private static HttpClient CreateClient(string? proxy)
    {
        if (string.IsNullOrWhiteSpace(proxy))
            return DefaultClient;

        var handler = new HttpClientHandler
        {
            Proxy = new WebProxy(proxy),
            UseProxy = true,
        };
        return new HttpClient(handler)
        {
            DefaultRequestHeaders =
            {
                { "User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36" },
                { "Referer", "https://space.bilibili.com/" },
            }
        };
    }

    private static async Task<(string Uid, string Nick, string Status)> CheckOneAsync(
        HttpClient client, string uid, int idx, int total, CancellationToken ct)
    {
        for (int attempt = 0; attempt < MaxRetries; attempt++)
        {
            if (ct.IsCancellationRequested) return (uid, "", "cancelled");
            try
            {
                var resp = await client.GetAsync($"{ApiUrl}?mid={uid}&photo=false", ct);

                if ((int)resp.StatusCode == 412)
                    return (uid, "", "blocked");

                if (!resp.IsSuccessStatusCode ||
                    resp.Content.Headers.ContentType?.MediaType?.Contains("json") != true)
                {
                    await Task.Delay(TimeSpan.FromSeconds(RetryDelaySec * (attempt + 1)), ct);
                    continue;
                }

                using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(ct));
                var root = doc.RootElement;
                var code = root.GetProperty("code").GetInt32();

                if (code == -412)
                    return (uid, "", "blocked");
                if (code != 0)
                    return (uid, "", $"error_{code}");

                var card = root.GetProperty("data").GetProperty("card");
                var level = 0;
                if (card.TryGetProperty("level_info", out var li) &&
                    li.TryGetProperty("current_level", out var lv))
                    level = lv.GetInt32();

                var nickname = card.GetProperty("name").GetString() ?? "";

                // Lv.0 + English-only name = likely unused account
                if (level == 0 && EnglishRe.IsMatch(nickname))
                    return (uid, nickname, "found");

                return (uid, nickname, $"Lv.{level}");
            }
            catch (TaskCanceledException) { return (uid, "", "cancelled"); }
            catch (HttpRequestException) { await Task.Delay(TimeSpan.FromSeconds(RetryDelaySec), ct); }
            catch { return (uid, "", "fail"); }
        }
        return (uid, "", "fail");
    }

    private void Log(string msg)
    {
        LogReceived?.Invoke(msg);
    }
}
