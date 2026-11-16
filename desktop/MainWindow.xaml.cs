using System.Reflection;
using System.Text.Json.Nodes;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using SCAssetBrowser.Services;
using Windows.Graphics;
using Windows.Storage.Pickers;

namespace SCAssetBrowser;

public sealed partial class MainWindow : Window
{
    Engine? _engine;
    AppState? _state;
    bool _polling;

    public MainWindow()
    {
        InitializeComponent();
        SystemBackdrop = new MicaBackdrop();
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        AppWindow.TitleBar.PreferredHeightOption = TitleBarHeightOption.Tall;
        AppWindow.Title = "SC Asset Browser";

        // AppWindow sizes are physical pixels: scale the layout's sizes by the display's DPI
        var scale = GetDpiForWindow(WinRT.Interop.WindowNative.GetWindowHandle(this)) / 96.0;
        var area = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary).WorkArea;
        var w = Math.Min((int)(1640 * scale), area.Width - 24);
        var h = Math.Min((int)(1000 * scale), area.Height - 24);
        AppWindow.MoveAndResize(new RectInt32(area.X + (area.Width - w) / 2, area.Y + (area.Height - h) / 2, w, h));
        if (AppWindow.Presenter is OverlappedPresenter p)
        {
            p.PreferredMinimumWidth = (int)(1180 * scale);
            p.PreferredMinimumHeight = (int)(700 * scale);
        }

        SetIcons();
        TitleSub.Text = "v" + (Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "");
        ApplyTheme(Prefs.Get("theme", "system"));
        Root.ActualThemeChanged += (_, _) => { UpdateCaptionColors(); _ = _state?.Viewer.SetTheme(IsDark); };
        Closed += (_, _) => _engine?.Dispose();
        _ = StartAsync();
        _ = CheckForUpdateAsync();
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern uint GetDpiForWindow(IntPtr hwnd);

    // ---------- startup ----------

    async Task StartAsync()
    {
        Splash.Visibility = Visibility.Visible;
        SplashError.IsOpen = false;
        SplashRing.IsActive = true;
        SplashText.Text = "Starting the engine…";
        try
        {
            // First run of a build: unpack the bundled tools. StarBreaker and
            // cgf-converter are needed before the engine starts (it indexes on start);
            // Blender, the big one, unpacks in the background meanwhile.
            var pending = await Task.Run(BundledTools.Pending);
            var small = pending.Where(t => t != "blender").ToList();
            if (small.Count > 0)
            {
                SplashText.Text = "Setting up the bundled tools (first run only)…";
                await Task.Run(() => BundledTools.Unpack(small));
                SplashText.Text = "Starting the engine…";
            }
            var blenderUnpack = pending.Contains("blender")
                ? Task.Run(() => { try { BundledTools.Unpack(["blender"]); } catch { } })
                : Task.CompletedTask;
            _engine?.Dispose();
            _engine = new Engine();
            var uri = await _engine.StartAsync();
            var api = new Api(uri);
            var jobs = new Jobs(api);
            Activity.ItemsSource = jobs.Items;
            SplashText.Text = "Loading the viewer…";
            var viewer = new Viewer(ViewerWeb);
            await viewer.InitAsync(api, IsDark);
            _state = new AppState(api, jobs, viewer);
            _state.InstallsLoaded += OnInstallsLoaded;
            _state.ChannelChanged += UpdateWorkspaceState;
            Browse.Init(_state, this);
            Characters.Init(_state, this);
            Inspector.Init(_state, this);
            BlenderPage.Init(_state, this);
            SettingsPage.Init(_state, this);
            SplashText.Text = "Looking for your game…";
            await _state.LoadInstallsAsync();
            Splash.Visibility = Visibility.Collapsed;
            Nav.IsEnabled = true;
            Nav.SelectedItem = NavBrowse;
            _ = CheckBlenderAsync(blenderUnpack);
        }
        catch (Exception e)
        {
            SplashRing.IsActive = false;
            SplashText.Text = "";
            SplashError.Message = e.Message + $"\n\nDetails: {_engine?.LogPath}";
            SplashError.IsOpen = true;
        }
    }

    void Retry_Click(object sender, RoutedEventArgs e) => _ = StartAsync();

    // ---------- updates ----------

    Updater.Release? _update;

    /// <summary>Each time the app opens: if GitHub has a newer release, say so in a bar.</summary>
    async Task CheckForUpdateAsync()
    {
        if (!Updater.CanSelfUpdate) return;
        Updater.CleanUp();
        try { _update = await Updater.CheckAsync(); }
        catch { return; }   // offline or GitHub unreachable: try again next time
        if (_update == null) return;
        UpdateBar.Title = $"Version {_update.Version.ToString(3)} is available";
        UpdateBar.Message = $"You have {Updater.Current.ToString(3)}. Updating downloads about {_update.Size / 1_000_000} MB and restarts the app.";
        UpdateNotes.NavigateUri = new Uri(_update.PageUrl);
        UpdateBar.IsOpen = true;
    }

    async void Update_Click(object sender, RoutedEventArgs e)
    {
        if (_update == null) return;
        UpdateButton.IsEnabled = false;
        UpdateBar.IsClosable = false;
        UpdateBar.Severity = InfoBarSeverity.Success;
        UpdateProgress.Value = 0;
        UpdateProgress.Visibility = Visibility.Visible;
        UpdateBar.Message = "Downloading…";
        var progress = new Progress<double>(f =>
        {
            UpdateProgress.Value = f;
            UpdateBar.Message = $"Downloading… {f:P0}";
        });
        try
        {
            await Updater.DownloadAsync(_update, progress);
            UpdateBar.Message = "Restarting…";
            _engine?.Dispose();     // before the swap script starts, so it can't hold the engine open
            _engine = null;
            Updater.SwapAndRestart();
        }
        catch (Exception ex)
        {
            UpdateProgress.Visibility = Visibility.Collapsed;
            UpdateButton.IsEnabled = true;
            UpdateBar.IsClosable = true;
            UpdateBar.Severity = InfoBarSeverity.Error;
            UpdateBar.Message = $"The update didn't install ({ex.Message}). You can download it from the release page under What's new.";
            if (_engine == null) _ = StartAsync();
            return;
        }
        Application.Current.Exit();
    }

    // ---------- Blender ----------

    public const string BlenderDownloadUrl = "https://download.blender.org/release/Blender5.2/blender-5.2.2-windows-x64.msi";
    bool _blenderBarClosed;

    /// <summary>After the bundled Blender (if any) has unpacked: if Blender still can't be
    /// found, offer the download once (first launch); the bar keeps saying so after that.</summary>
    async Task CheckBlenderAsync(Task blenderUnpack)
    {
        try
        {
            if (!blenderUnpack.IsCompleted)
            {
                await blenderUnpack;
                await _state!.LoadInstallsAsync();      // pick up the freshly unpacked Blender
            }
            if (_state == null || _state.HaveBlender || Prefs.Get("blender_prompted") == "1") return;
            Prefs.Set("blender_prompted", "1");
            var dialog = new ContentDialog
            {
                XamlRoot = Content.XamlRoot,
                Title = "Blender isn't installed",
                Content = new TextBlock
                {
                    TextWrapping = TextWrapping.Wrap,
                    Text = "Browsing, previews, 3D printing and most exports work without it. Landing gear up, "
                         + "deployed weapons, FBX / USDZ / .blend export and the Blender sandbox need Blender "
                         + "(free, from blender.org).\n\nInstall it, then click Check again in the bar at the top, "
                         + "or restart the app.",
                },
                PrimaryButtonText = "Download Blender 5.2",
                CloseButtonText = "Not now",
                DefaultButton = ContentDialogButton.Primary,
            };
            if (await dialog.ShowAsync() == ContentDialogResult.Primary)
                await Windows.System.Launcher.LaunchUriAsync(new Uri(BlenderDownloadUrl));
        }
        catch { /* a missing prompt is not worth an error */ }
    }

    void BlenderBar_Closed(InfoBar sender, InfoBarClosedEventArgs args) => _blenderBarClosed = true;

    async void BlenderCheck_Click(object sender, RoutedEventArgs e)
    {
        if (_state == null) return;
        try { await _state.LoadInstallsAsync(); }
        catch (Exception ex) { ShowError("Couldn't check for Blender", ex.Message); }
        if (_state.HaveBlender) ShowInfo("Blender found", _state.Tools.GetValueOrDefault("blender") ?? "");
    }

    void SetIcons()
    {
        var asm = Assembly.GetExecutingAssembly();
        try
        {
            var ico = Path.Combine(Paths.Home, "app.ico");
            using (var s = asm.GetManifestResourceStream("app.ico"))
            using (var f = File.Create(ico)) s!.CopyTo(f);
            AppWindow.SetIcon(ico);
        }
        catch { }
        _ = LoadImage(TitleIcon, "app-256.png");
        _ = LoadImage(SplashIcon, "app-256.png");
    }

    static async Task LoadImage(Image target, string resource)
    {
        using var s = Assembly.GetExecutingAssembly().GetManifestResourceStream(resource);
        if (s == null) return;
        var ms = new MemoryStream();
        await s.CopyToAsync(ms);
        ms.Position = 0;
        var bmp = new BitmapImage();
        await bmp.SetSourceAsync(ms.AsRandomAccessStream());
        target.Source = bmp;
    }

    // ---------- theme ----------

    public bool IsDark => Root.ActualTheme == ElementTheme.Dark;

    public void ApplyTheme(string theme)
    {
        Root.RequestedTheme = theme switch { "light" => ElementTheme.Light, "dark" => ElementTheme.Dark, _ => ElementTheme.Default };
        UpdateCaptionColors();
    }

    void UpdateCaptionColors()
    {
        var tb = AppWindow.TitleBar;
        var dark = IsDark;
        tb.ButtonBackgroundColor = Colors.Transparent;
        tb.ButtonInactiveBackgroundColor = Colors.Transparent;
        tb.ButtonForegroundColor = dark ? Colors.White : Colors.Black;
        tb.ButtonInactiveForegroundColor = dark ? ColorHelper.FromArgb(255, 150, 150, 150) : ColorHelper.FromArgb(255, 120, 120, 120);
        tb.ButtonHoverBackgroundColor = dark ? ColorHelper.FromArgb(24, 255, 255, 255) : ColorHelper.FromArgb(20, 0, 0, 0);
        tb.ButtonHoverForegroundColor = tb.ButtonForegroundColor;
        tb.ButtonPressedBackgroundColor = dark ? ColorHelper.FromArgb(40, 255, 255, 255) : ColorHelper.FromArgb(36, 0, 0, 0);
        tb.ButtonPressedForegroundColor = tb.ButtonForegroundColor;
    }

    // ---------- navigation ----------

    void Nav_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        var tag = args.IsSettingsSelected ? "settings" : (args.SelectedItem as NavigationViewItem)?.Tag as string ?? "browse";
        WorkArea.Visibility = tag is "browse" or "characters" ? Visibility.Visible : Visibility.Collapsed;
        BlenderPage.Visibility = tag == "blender" ? Visibility.Visible : Visibility.Collapsed;
        SettingsPage.Visibility = tag == "settings" ? Visibility.Visible : Visibility.Collapsed;
        Browse.Visibility = tag == "browse" ? Visibility.Visible : Visibility.Collapsed;
        Characters.Visibility = tag == "characters" ? Visibility.Visible : Visibility.Collapsed;
        if (tag == "characters") Characters.Activate();
        if (tag == "blender") BlenderPage.Activate();
        if (tag == "settings") SettingsPage.Activate();
    }

    // ---------- installs ----------

    void OnInstallsLoaded()
    {
        var s = _state!;
        InstallBox.SelectionChanged -= InstallBox_SelectionChanged;
        InstallBox.ItemsSource = s.Installs;
        InstallBox.SelectedItem = s.CurrentInstall;
        InstallBox.SelectionChanged += InstallBox_SelectionChanged;
        ToolsBar.IsOpen = !s.HaveGameTools;
        BlenderBar.IsOpen = !s.HaveBlender && !_blenderBarClosed;
        UpdateWorkspaceState();
        SettingsPage.Refresh();
        BlenderPage.Refresh();
        Inspector.RefreshFormats();
    }

    void InstallBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (InstallBox.SelectedItem is Install i) _state?.SetChannel(i.Channel);
    }

    /// <summary>Show the empty, indexing or not-indexed state over the workspace, or the workspace itself.</summary>
    public void UpdateWorkspaceState()
    {
        var s = _state;
        if (s == null) return;
        var inst = s.CurrentInstall;
        BuildText.Text = inst != null ? $"build {inst.Change} · {inst.Path}" : "";
        ReindexButton.IsEnabled = inst != null && s.HaveGameTools && inst.IndexState != "running";
        StateActions.Children.Clear();
        StateRing.IsActive = false;
        StateRing.Visibility = Visibility.Collapsed;
        StateIcon.Visibility = Visibility.Visible;

        if (inst == null)
        {
            ShowState("", "No Star Citizen install found",
                "Choose your StarCitizen folder: the one that contains LIVE (and PTU, if you have it)." +
                (s.Searched.Count > 0 ? "\n\nLooked in: " + string.Join(", ", s.Searched) : ""));
            StateActions.Children.Add(AccentButton("Choose game folder…", async () => await ChooseGameFolderAsync()));
            return;
        }
        if (inst.Indexed)
        {
            StateOverlay.Visibility = Visibility.Collapsed;
            return;
        }
        if (inst.IndexState == "running")
        {
            ShowState(null, $"Indexing {inst.Channel} {inst.Version}…",
                "Reading the game's records. The first time takes a few minutes; this updates when it's done.");
            StateRing.IsActive = true;
            StateRing.Visibility = Visibility.Visible;
            StateIcon.Visibility = Visibility.Collapsed;
            _ = PollIndexing();
        }
        else if (inst.IndexState == "error")
        {
            ShowState("", $"Indexing {inst.Channel} failed", inst.IndexError);
            StateActions.Children.Add(AccentButton("Try again", Reindex));
        }
        else
        {
            ShowState("", $"{inst.Channel} {inst.Version} isn't indexed yet",
                s.HaveGameTools ? "Indexing reads the game's records so you can search them. It takes a few minutes."
                                : "Download the game tools first (above), then index the game.");
            if (s.HaveGameTools) StateActions.Children.Add(AccentButton("Index now", Reindex));
        }
    }

    void ShowState(string? glyph, string title, string text)
    {
        if (glyph != null) StateIcon.Glyph = glyph;
        StateTitle.Text = title;
        StateText.Text = text;
        StateOverlay.Visibility = Visibility.Visible;
    }

    static Button AccentButton(string text, Func<Task> onClick)
    {
        var b = new Button { Content = text, Style = (Style)Application.Current.Resources["AccentButtonStyle"] };
        b.Click += async (_, _) => await onClick();
        return b;
    }

    async Task PollIndexing()
    {
        if (_polling) return;
        _polling = true;
        try
        {
            while (_state?.CurrentInstall is { IndexState: "running" })
            {
                await Task.Delay(4000);
                await _state.LoadInstallsAsync();
            }
        }
        catch { }
        finally { _polling = false; }
    }

    void Reindex_Click(object sender, RoutedEventArgs e) => _ = Reindex();

    public async Task Reindex()
    {
        var s = _state!;
        if (s.Channel == null) return;
        try
        {
            var job = await s.Api.StartJobAsync("api/update", new JsonObject { ["channel"] = s.Channel });
            await s.LoadInstallsAsync();
            await s.Jobs.RunAsync(job);
            await s.LoadInstallsAsync();
            Browse.Refresh();
        }
        catch (Exception ex)
        {
            await s.LoadInstallsAsync();
            if (ex is not JobFailedException) ShowError("Couldn't update the index", ex.Message);
        }
    }

    void Tools_Click(object sender, RoutedEventArgs e) => _ = DownloadTools();

    public async Task DownloadTools()
    {
        var s = _state!;
        ToolsButton.IsEnabled = false;
        try
        {
            var job = await s.Api.StartJobAsync("api/tools/update", []);
            await s.Jobs.RunAsync(job);
        }
        catch (Exception ex) when (ex is not JobFailedException) { ShowError("Downloading the tools failed", ex.Message); }
        catch (JobFailedException) { }
        finally
        {
            ToolsButton.IsEnabled = true;
            await s.LoadInstallsAsync();
        }
    }

    public async Task ChooseGameFolderAsync()
    {
        var picker = new FolderPicker { SuggestedStartLocation = PickerLocationId.ComputerFolder };
        picker.FileTypeFilter.Add("*");
        WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        var folder = await picker.PickSingleFolderAsync();
        if (folder == null) return;
        try
        {
            await _state!.Api.PostAsync("api/roots", new JsonObject { ["path"] = folder.Path });
            await _state.LoadInstallsAsync();
        }
        catch (Exception ex) { ShowError("That folder doesn't contain a game install", ex.Message); }
    }

    void ResetView_Click(object sender, RoutedEventArgs e) => _ = _state?.Viewer.ResetView();

    public void ClearBrowseSelection() => Browse.ClearSelection();

    // ---------- activity ----------

    public void ShowError(string title, string message)
    {
        if (_state == null) return;
        _state.Jobs.Items.Insert(0, new JobItem { Label = title, Detail = message, State = "error", IsNotice = true });
    }

    public void ShowInfo(string title, string message)
    {
        if (_state == null) return;
        _state.Jobs.Items.Insert(0, new JobItem { Label = title, Detail = message, State = "done", IsNotice = true });
    }

    void Job_Close(InfoBar sender, object args)
    {
        if (sender.DataContext is JobItem j) _state?.Jobs.Items.Remove(j);
    }
}
