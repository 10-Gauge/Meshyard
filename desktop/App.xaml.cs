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
        // On PCs with a second, built-in graphics chip, Windows can hand the viewer to that
        // one instead of the real card: ask for the fast one.
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS")))
            Environment.SetEnvironmentVariable("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--force_high_performance_gpu");
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
