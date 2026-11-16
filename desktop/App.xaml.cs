using Microsoft.UI.Xaml;
using SCAssetBrowser.Services;

namespace SCAssetBrowser;

public partial class App : Application
{
    public static MainWindow? Main { get; private set; }

    public App()
    {
        // WebView2 keeps its profile with the toolkit's own data, not beside the exe.
        Environment.SetEnvironmentVariable("WEBVIEW2_USER_DATA_FOLDER", Path.Combine(Paths.Home, "webview2"));
        InitializeComponent();
        UnhandledException += (_, e) =>
        {
            Paths.WriteLog("app-error.log", e.Exception.ToString());
            e.Handled = true;
            Main?.ShowError("Something went wrong", e.Exception.Message);
        };
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        Main = new MainWindow();
        Main.Activate();
    }
}
