using Avalonia.Controls;
using Avalonia.Threading;
using BiliSearch.UI.ViewModels;

namespace BiliSearch.UI.Views;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
        Closing += (_, _) =>
        {
            if (DataContext is IDisposable d) d.Dispose();
        };
    }
}
