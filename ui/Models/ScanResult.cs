using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace BiliSearch.UI.Models;

public class ScanResult : INotifyPropertyChanged
{
    private string _uid = "";
    private string _nickname = "";
    private string _status = "";

    public string Uid
    {
        get => _uid;
        set { _uid = value; OnPropertyChanged(); }
    }

    public string Nickname
    {
        get => _nickname;
        set { _nickname = value; OnPropertyChanged(); }
    }

    public string Status
    {
        get => _status;
        set { _status = value; OnPropertyChanged(); }
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
