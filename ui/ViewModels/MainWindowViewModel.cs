using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows.Input;
using Avalonia.Threading;
using BiliSearch.UI.Models;
using BiliSearch.UI.Services;

namespace BiliSearch.UI.ViewModels;

public class MainWindowViewModel : INotifyPropertyChanged, IDisposable
{
    private readonly ScanEngine _engine = new();

    // Tab
    private int _selectedTab;
    public int SelectedTab
    {
        get => _selectedTab;
        set { _selectedTab = value; OnPropertyChanged(); OnPropertyChanged(nameof(IsSuffixTab)); OnPropertyChanged(nameof(IsPalindromeTab)); }
    }
    public bool IsSuffixTab => SelectedTab == 0;
    public bool IsPalindromeTab => SelectedTab == 1;

    // Suffix tab
    private string _suffix = "";
    public string Suffix { get => _suffix; set { _suffix = value; OnPropertyChanged(); } }

    // Template tab
    private string _template = "";
    public string Template { get => _template; set { _template = value; OnPropertyChanged(); } }

    // Palindrome tab
    private string _uidLo = "";
    public string UidLo { get => _uidLo; set { _uidLo = value; OnPropertyChanged(); } }
    private string _uidHi = "";
    public string UidHi { get => _uidHi; set { _uidHi = value; OnPropertyChanged(); } }

    // Proxy
    private string _proxyAddress = "";
    public string ProxyAddress { get => _proxyAddress; set { _proxyAddress = value; OnPropertyChanged(); } }

    private bool _proxyNone = true;
    public bool ProxyNone
    {
        get => _proxyNone;
        set { if (value) { _proxyNone = true; _proxyManual = false; _proxyPool = false; OnPropertyChanged(); OnPropertyChanged(nameof(ProxyManual)); OnPropertyChanged(nameof(ProxyPool)); OnPropertyChanged(nameof(IsManualProxy)); } }
    }
    private bool _proxyManual;
    public bool ProxyManual
    {
        get => _proxyManual;
        set { if (value) { _proxyNone = false; _proxyManual = true; _proxyPool = false; OnPropertyChanged(); OnPropertyChanged(nameof(ProxyNone)); OnPropertyChanged(nameof(ProxyPool)); OnPropertyChanged(nameof(IsManualProxy)); } }
    }
    private bool _proxyPool;
    public bool ProxyPool
    {
        get => _proxyPool;
        set { if (value) { _proxyNone = false; _proxyManual = false; _proxyPool = true; OnPropertyChanged(); OnPropertyChanged(nameof(ProxyNone)); OnPropertyChanged(nameof(ProxyManual)); OnPropertyChanged(nameof(IsManualProxy)); } }
    }
    public bool IsManualProxy => _proxyManual;

    // Status
    private string _statusText = "就绪";
    public string StatusText { get => _statusText; set { _statusText = value; OnPropertyChanged(); } }
    private double _progress;
    public double Progress { get => _progress; set { _progress = value; OnPropertyChanged(); } }
    private bool _isScanning;
    public bool IsScanning
    {
        get => _isScanning;
        set { _isScanning = value; OnPropertyChanged(); OnPropertyChanged(nameof(CanStart)); OnPropertyChanged(nameof(CanStop)); }
    }
    public bool CanStart => !IsScanning;
    public bool CanStop => IsScanning;

    // Log
    public ObservableCollection<string> LogLines { get; } = [];
    private string _logText = "";
    public string LogText { get => _logText; set { _logText = value; OnPropertyChanged(); } }

    // Results
    public ObservableCollection<ScanResult> Results { get; } = [];

    // Commands
    public ICommand StartCommand { get; }
    public ICommand StopCommand { get; }

    private CancellationTokenSource? _cts;

    public MainWindowViewModel()
    {
        StartCommand = new RelayCommand(_ => _ = StartScanAsync());
        StopCommand = new RelayCommand(_ => StopScan());

        // Wire engine events → UI thread
        _engine.LogReceived += msg =>
            Dispatcher.UIThread.Post(() =>
            {
                LogLines.Add(msg);
                while (LogLines.Count > 2000) LogLines.RemoveAt(0);
                LogText = string.Join('\n', LogLines);
            });

        _engine.Progress += (done, total, found) =>
            Dispatcher.UIThread.Post(() =>
            {
                Progress = total > 0 ? (double)done / total * 100 : 0;
                StatusText = $"{done} / {total}  —  找到 {found} 个";
            });

        _engine.ScanFinished += completed =>
            Dispatcher.UIThread.Post(() =>
            {
                if (completed)
                {
                    var found = _engine.Results.Count(r => r.Status == "found");
                    StatusText = $"完成 — 共 {_engine.Done} 个，找到 {found} 个";
                }
                else
                {
                    StatusText = "已停止";
                }
                RefreshResults();
                IsScanning = false;
            });
    }

    private async Task StartScanAsync()
    {
        List<string> uids;

        if (SelectedTab == 0) // suffix
        {
            if (string.IsNullOrWhiteSpace(Suffix) || Suffix.Length != 4 || !int.TryParse(Suffix, out _))
            {
                StatusText = "错误：后缀必须是4位数字";
                return;
            }
            uids = ScanEngine.GenerateSuffixUids(Suffix);
        }
        else if (SelectedTab == 1) // template
        {
            if (string.IsNullOrWhiteSpace(Template))
            {
                StatusText = "错误：请输入模板";
                return;
            }
            var tpl = Template.Trim().ToUpper();
            if (!tpl.Contains('X'))
            {
                StatusText = "错误：模板必须包含至少一个 X";
                return;
            }
            int xCount = tpl.Count(c => c == 'X');
            if (xCount > 7)
            {
                StatusText = $"错误：通配符 X 过多 ({xCount} 个)，最多 7 个";
                return;
            }
            uids = ScanEngine.GenerateFromTemplate(tpl);
        }
        else // palindrome (tab 2)
        {
            uids = ScanEngine.GenerateAllPalindromes();

            if (!string.IsNullOrWhiteSpace(UidLo) && long.TryParse(UidLo.Trim(), out var lo))
                uids = [.. uids.Where(u => long.Parse(u) >= lo)];
            if (!string.IsNullOrWhiteSpace(UidHi) && long.TryParse(UidHi.Trim(), out var hi))
                uids = [.. uids.Where(u => long.Parse(u) <= hi)];
        }

        if (uids.Count == 0)
        {
            StatusText = "没有符合条件的 UID";
            return;
        }

        // Proxy
        string? fixedProxy = null;
        bool usePool = false;
        if (_proxyManual)
        {
            if (string.IsNullOrWhiteSpace(ProxyAddress))
            {
                StatusText = "错误：请输入代理地址";
                return;
            }
            fixedProxy = ProxyAddress.Trim();
        }
        else if (_proxyPool)
        {
            usePool = true;
        }

        // Reset UI
        LogText = "";
        LogLines.Clear();
        Results.Clear();
        Progress = 0;
        IsScanning = true;
        _cts = new CancellationTokenSource();

        StatusText = $"扫描中... (共 {uids.Count} 个 UID)";

        try
        {
            await _engine.StartAsync(uids, fixedProxy, usePool, _cts.Token);
        }
        catch (OperationCanceledException) { StatusText = "已取消"; }
        catch (Exception ex) { StatusText = $"异常: {ex.Message}"; }
        finally { IsScanning = false; }
    }

    private void StopScan()
    {
        _engine.Stop();
        _cts?.Cancel();
        StatusText = "已停止";
        IsScanning = false;
    }

    private void RefreshResults()
    {
        Results.Clear();
        // "found" first, then by UID
        var sorted = _engine.Results
            .OrderByDescending(r => r.Status == "found")
            .ThenBy(r => r.Uid);
        foreach (var (uid, nick, status) in sorted)
        {
            Results.Add(new ScanResult
            {
                Uid = uid,
                Nickname = string.IsNullOrEmpty(nick) ? "—" : nick,
                Status = status
            });
        }
    }

    public void Dispose()
    {
        _cts?.Cancel();
        _cts?.Dispose();
    }

    // --- INotifyPropertyChanged ---
    public event PropertyChangedEventHandler? PropertyChanged;
    private void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

/// <summary>Minimal ICommand implementation.</summary>
public class RelayCommand(Action<object?> execute, Func<object?, bool>? canExecute = null) : ICommand
{
    public event EventHandler? CanExecuteChanged;
    public bool CanExecute(object? parameter) => canExecute?.Invoke(parameter) ?? true;
    public void Execute(object? parameter) => execute(parameter);
    public void RaiseCanExecuteChanged() => CanExecuteChanged?.Invoke(this, EventArgs.Empty);
}
