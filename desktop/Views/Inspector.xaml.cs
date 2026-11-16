using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using SCAssetBrowser.Services;

namespace SCAssetBrowser.Views;

public sealed partial class Inspector : UserControl
{
    AppState? _state;
    MainWindow? _main;
    bool _defaultsApplied;
    int _loadSeq;
    string _variant = "preview";
    bool _settingGear;
    readonly List<ThinRow> _thin = [];

    sealed record ThinRow(string Name, ComboBox Action, NumberBox Mm);

    public Inspector() => InitializeComponent();

    public void Init(AppState state, MainWindow main)
    {
        _state = state;
        _main = main;
        Fill(ModeBox, ("solid", "Solid, watertight"), ("parts", "Separate parts (kit)"));
        Fill(FormatBox, ("3mf", "3MF"), ("stl", "STL"), ("obj", "OBJ"), ("ply", "PLY"));
        Fill(StandBox, ("none", "No stand"), ("peg", "Separate stand, pegs into a socket"),
             ("attached", "Stand printed with the model"));
        Fill(ShapeBox, ("round", "Round"), ("rect", "Rectangle with name plate"));
        Fill(LogoBox, ("auto", "The manufacturer's logo"), ("", "No logo"));
        state.SelectionChanged += OnSelection;
        state.InstallsLoaded += ApplyDefaults;
        state.InstallsLoaded += () => _ = LoadLogos();
    }

    static void Fill(ComboBox box, params (string Id, string Label)[] items)
    {
        box.ItemsSource = items.Select(i => new Choice(i.Id, i.Label)).ToList();
        box.SelectedIndex = 0;
    }

    static void Pick(ComboBox box, string id)
    {
        var item = (box.ItemsSource as IEnumerable<Choice>)?.FirstOrDefault(c => c.Id == id);
        if (item != null) box.SelectedItem = item;
    }

    static string Picked(ComboBox box) => (box.SelectedItem as Choice)?.Id ?? "";

    bool _logosLoaded;

    /// <summary>Every manufacturer the game has a logo for, after "auto" and "none".</summary>
    async Task LoadLogos()
    {
        if (_logosLoaded || _state?.Channel == null || _state.CurrentInstall is not { Indexed: true }) return;
        try
        {
            var d = await _state.Api.GetAsync("api/logos", ("channel", _state.Channel));
            var keep = Picked(LogoBox);
            var items = new List<Choice> { new("auto", "The manufacturer's logo"), new("", "No logo") };
            items.AddRange(d.Items("items").Select(i => new Choice(i.Str("code"), i.Str("name"))));
            LogoBox.ItemsSource = items;
            Pick(LogoBox, keep);
            _logosLoaded = true;
        }
        catch { /* the two built-in choices still work */ }
    }

    void ApplyDefaults()
    {
        if (_defaultsApplied || _state!.PrintDefaults.Count == 0) return;
        _defaultsApplied = true;
        var p = _state.PrintDefaults;
        SizeBox.Value = p.Num("size_mm", 150);
        VoxelBox.Value = p.Num("voxel_mm", 0.2);
        NozzleBox.Value = p.Num("nozzle_mm", 0.4);
        MinNBox.Value = p.Num("min_feature_nozzles", 3);
        StandHBox.Value = p.Num("stand_height_mm", 30);
        TiltBox.Value = p.Num("tilt_deg", 0);
        HollowBox.Value = p.Num("hollow_mm", 0);
        ThickenSwitch.IsOn = p.Bool("thicken", true);
        Pick(ModeBox, p.Str("mode", "solid"));
        Pick(FormatBox, p.Str("format", "3mf"));
        Pick(StandBox, p.Str("stand", "none"));
        Pick(ShapeBox, p.Str("stand_shape", "round"));
        UpdateStrength();
        UpdateStand();
    }

    public void RefreshFormats()
    {
        var s = _state!;
        var keep = (ExportFormatBox.SelectedItem as ComboBoxItem)?.Tag as string ?? Prefs.Get("exportFormat", "glb");
        var items = s.ExportFormats.Select(f =>
        {
            var needsBlender = f.Label.Contains("Blender");
            return new ComboBoxItem
            {
                Content = f.Label + (needsBlender && !s.HaveBlender ? " (set up Blender first)" : ""),
                Tag = f.Id,
                IsEnabled = !needsBlender || s.HaveBlender,
            };
        }).ToList();
        ExportFormatBox.ItemsSource = items;
        ExportFormatBox.SelectedItem = items.FirstOrDefault(i => (string)i.Tag == keep && i.IsEnabled)
                                       ?? items.FirstOrDefault(i => (string)i.Tag == "glb");
    }

    void ExportFormat_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (ExportFormatBox.SelectedItem is ComboBoxItem { Tag: string id }) Prefs.Set("exportFormat", id);
    }

    // ---------- selection ----------

    void OnSelection(Target? t)
    {
        Placeholder.Visibility = t == null ? Visibility.Visible : Visibility.Collapsed;
        Details.Visibility = t == null ? Visibility.Collapsed : Visibility.Visible;
        if (t == null) return;
        NameText.Text = t.Name;
        MetaText.Text = t.Meta;
        DescText.Text = t.Description;
        DescText.Visibility = string.IsNullOrWhiteSpace(t.Description) ? Visibility.Collapsed : Visibility.Visible;
        AssetActions.Visibility = t.IsFigure ? Visibility.Collapsed : Visibility.Visible;
        // ships have landing gear; the switch keeps its setting from ship to ship
        GearRow.Visibility = t.Asset?.Category == "spaceships" ? Visibility.Visible : Visibility.Collapsed;
        WeaponsRow.Visibility = GearRow.Visibility;
        PrintResult.IsOpen = false;
        ExportResult.IsOpen = false;
        PlateText.Text = t.Name;
        if (t.IsFigure && Picked(LogoBox) == "auto") Pick(LogoBox, "");  // a figure has no maker of its own
        ClearThin();
        SizeBox.Header = t.IsFigure ? "Height (mm)" : "Longest side (mm)";
        if (t.IsFigure)
        {
            // figures: size is the height; finer detail and a stand under the feet suit them
            if (SizeBox.Value > 120) SizeBox.Value = 75;
            if (VoxelBox.Value > 0.12) VoxelBox.Value = 0.1;
            if (Picked(StandBox) == "none") Pick(StandBox, "attached");
            _loadSeq++;
            _ = _state!.Viewer.Show(_state.Api.Url(t.FigureUrl!));
        }
        else _ = LoadModel("preview");
    }

    bool GearUp => GearSwitch.IsOn && GearRow.Visibility == Visibility.Visible && _state?.Selected?.Asset != null;
    bool WeaponsOut => WeaponsSwitch.IsOn && WeaponsRow.Visibility == Visibility.Visible && _state?.Selected?.Asset != null;

    void Gear_Toggled(object sender, RoutedEventArgs e)
    {
        if (_settingGear || _state?.Selected?.Asset == null) return;
        PrintResult.IsOpen = false;
        ExportResult.IsOpen = false;
        _ = LoadModel(_variant);
    }

    async Task LoadModel(string variant, bool spawn = false)
    {
        var s = _state!;
        var t = s.Selected;
        if (t?.Asset == null) return;
        var seq = spawn ? _loadSeq : ++_loadSeq;
        var gearUp = GearUp && variant != "animated";
        var weaponsOut = WeaponsOut && variant != "animated";
        if (!spawn)
        {
            _variant = variant;
            await s.Viewer.Clear(gearUp || weaponsOut
                ? (gearUp ? "Retracting the landing gear" : "Deploying the weapons")
                  + "… the first time for each ship takes a little longer."
                : "Exporting the model… big ships can take a minute or two the first time.");
        }
        try
        {
            var job = await s.Api.StartJobAsync("api/model", new JsonObject
            {
                ["channel"] = s.Channel, ["id"] = t.Asset.Id, ["variant"] = variant, ["spawn"] = spawn,
                ["gear"] = gearUp ? "up" : "down", ["weapons"] = weaponsOut ? "out" : "",
            });
            var r = await s.Jobs.RunAsync(job);
            if (!spawn && seq == _loadSeq && r.Str("url") is { Length: > 0 } url) await s.Viewer.Show(s.Api.Url(url));
        }
        catch (Exception e)
        {
            if (gearUp && e.Message.Contains("landing gear", StringComparison.OrdinalIgnoreCase) && !e.Message.Contains("need Blender"))
            {
                // this ship can't retract (or Blender is missing): say why, and show it gear down
                _main!.ShowError("Landing gear up isn't available", LastLines(e.Message));
                _settingGear = true;
                GearSwitch.IsOn = false;
                _settingGear = false;
                if (!spawn && seq == _loadSeq) _ = LoadModel(variant);
                return;
            }
            if (weaponsOut && e.Message.Contains("weapons", StringComparison.OrdinalIgnoreCase))
            {
                // most ships' guns are fixed: say so, and show it as exported
                _main!.ShowError("This ship has no deployable weapons", LastLines(e.Message));
                _settingGear = true;
                WeaponsSwitch.IsOn = false;
                _settingGear = false;
                if (!spawn && seq == _loadSeq) _ = LoadModel(variant);
                return;
            }
            if (!spawn && seq == _loadSeq) await s.Viewer.Say("Export failed: " + LastLines(e.Message));
            else if (e is not JobFailedException) _main!.ShowError("Couldn't spawn it", e.Message);
        }
    }

    void Preview_Click(object sender, RoutedEventArgs e) => _ = LoadModel("preview");
    void Full_Click(object sender, RoutedEventArgs e) => _ = LoadModel("full");
    void Spawn_Click(object sender, RoutedEventArgs e) => _ = LoadModel("full", true);
    void SpawnAnim_Click(object sender, RoutedEventArgs e) => _ = LoadModel("animated", true);

    static string LastLines(string s) => string.Join(" ", s.Split('\n', StringSplitOptions.RemoveEmptyEntries).TakeLast(3));

    // ---------- print ----------

    void Strength_Changed(NumberBox sender, NumberBoxValueChangedEventArgs args) => UpdateStrength();

    void UpdateStrength()
    {
        var mm = Safe(NozzleBox.Value) * Safe(MinNBox.Value);
        MinMmText.Text = $"Parts thinner than {mm:0.##} mm are thickened.";
    }

    static double Safe(double v) => double.IsNaN(v) ? 0 : v;

    void Stand_Changed(object sender, SelectionChangedEventArgs e) => UpdateStand();

    void UpdateStand()
    {
        var none = Picked(StandBox) == "none";
        ShapeBox.IsEnabled = !none;
        StandHBox.IsEnabled = !none;
        PlateText.IsEnabled = PlateText2.IsEnabled = LogoBox.IsEnabled = !none && Picked(ShapeBox) == "rect";
    }

    JsonObject PrintOptions()
    {
        var o = new JsonObject
        {
            ["mode"] = Picked(ModeBox), ["format"] = Picked(FormatBox), ["stand"] = Picked(StandBox),
            ["thicken"] = ThickenSwitch.IsOn, ["stand_shape"] = Picked(ShapeBox), ["stand_text"] = PlateText.Text.Trim(),
            ["stand_text2"] = PlateText2.Text.Trim(), ["stand_logo"] = Picked(LogoBox),
            ["size_mm"] = Safe(SizeBox.Value), ["voxel_mm"] = Safe(VoxelBox.Value), ["nozzle_mm"] = Safe(NozzleBox.Value),
            ["min_feature_nozzles"] = Safe(MinNBox.Value), ["stand_height_mm"] = Safe(StandHBox.Value),
            ["tilt_deg"] = Safe(TiltBox.Value), ["hollow_mm"] = Safe(HollowBox.Value),
        };
        var overrides = new JsonObject();
        foreach (var r in _thin)
        {
            var action = Picked(r.Action);
            if (action != "auto") overrides[r.Name] = new JsonObject { ["action"] = action, ["mm"] = Safe(r.Mm.Value) };
        }
        o["part_overrides"] = overrides;
        return o;
    }

    JsonObject Request(Target t, JsonObject? extra = null)
    {
        var body = extra ?? [];
        body["channel"] = _state!.Channel;
        t.AddTo(body);
        if (GearUp) body["gear"] = "up";
        if (WeaponsOut) body["weapons"] = "out";
        return body;
    }

    async void Print_Click(object sender, RoutedEventArgs e)
    {
        var s = _state!;
        var t = s.Selected;
        if (t == null) return;
        PrintButton.IsEnabled = false;
        PrintResult.IsOpen = false;
        PrintBusy.Visibility = Visibility.Visible;
        PrintBusyText.Text = "Working… large ships can take a few minutes.";
        try
        {
            var job = await s.Api.StartJobAsync("api/print", Request(t, new JsonObject { ["options"] = PrintOptions() }));
            var r = await s.Jobs.RunAsync(job, line => PrintBusyText.Text = line.Trim());
            var size = string.Join(" × ", (r["size_mm"] as JsonArray ?? []).Select(x => $"{x!.GetValue<double>():0}"));
            var parts = new List<string> { $"{r.Long("triangles"):N0} triangles" };
            if (r.Str("mode") == "solid")
            {
                parts.Add(r.Bool("watertight") ? "watertight" : "not watertight");
                parts.Add($"{r.Num("volume_cm3"):0.##} cm³");
                parts.Add($"detail {r.Num("voxel_mm"):0.##} mm");
            }
            else parts.Add($"{r.Long("watertight_bodies")}/{r.Long("bodies")} parts watertight");
            if (r.Long("thickened") > 0) parts.Add($"{r.Long("thickened")} thin parts thickened to {r.Num("min_feature_mm"):0.##} mm");
            var files = r.Items("files").Select(f => f.GetValue<string>()).ToList();
            ShowResult(PrintResult, InfoBarSeverity.Success, $"{size} mm ({r.Str("scale")})", string.Join(" · ", parts), files);
        }
        catch (Exception ex)
        {
            ShowResult(PrintResult, InfoBarSeverity.Error, "The print export failed", LastLines(ex.Message), []);
        }
        finally
        {
            PrintBusy.Visibility = Visibility.Collapsed;
            PrintButton.IsEnabled = true;
        }
    }

    static void ShowResult(InfoBar bar, InfoBarSeverity sev, string title, string message, List<string> files)
    {
        bar.Severity = sev;
        bar.Title = title;
        bar.Message = message + (files.Count > 0 ? "\n" + string.Join("\n", files.Select(Path.GetFileName)) : "");
        if (files.Count > 0)
        {
            var b = new Button { Content = "Show in folder" };
            b.Click += (_, _) => Paths.Reveal(files[0]);
            bar.ActionButton = b;
        }
        else bar.ActionButton = null;
        bar.IsOpen = true;
    }

    // ---------- thin parts ----------

    void ClearThin()
    {
        _thin.Clear();
        ThinRows.Children.Clear();
        ThinPanel.Visibility = Visibility.Collapsed;
    }

    async void Thin_Click(object sender, RoutedEventArgs e)
    {
        var s = _state!;
        var t = s.Selected;
        if (t == null) return;
        ClearThin();
        ThinPanel.Visibility = Visibility.Visible;
        ThinSummary.Text = "Measuring parts at print size…";
        ThinButton.IsEnabled = false;
        try
        {
            var job = await s.Api.StartJobAsync("api/print/analyze", Request(t, new JsonObject { ["options"] = PrintOptions() }));
            var r = await s.Jobs.RunAsync(job);
            if (s.Selected != t) return;
            var min = r.Num("min_feature_mm");
            var slender = r.Items("slender").ToList();
            var size = string.Join(" × ", (r["size_mm"] as JsonArray ?? []).Select(x => x!.ToString()));
            ThinSummary.Text = $"{size} mm at {r.Str("scale")}. " + (slender.Count > 0
                ? $"Orange parts are thinner than {min:0.##} mm. Auto thickens them; you can pick a size or remove a part to replace it with wire or rod."
                : "No slender parts found.");
            foreach (var p in slender) AddThinRow(p, min);
        }
        catch (Exception ex) { ThinSummary.Text = LastLines(ex.Message); }
        finally { ThinButton.IsEnabled = true; }
    }

    void AddThinRow(JsonNode p, double min)
    {
        var name = p.Str("name");
        var thick = p.Num("thickness_mm");
        var grid = new Grid { ColumnSpacing = 8, Padding = new Thickness(10, 8, 10, 8), CornerRadius = new CornerRadius(4),
                              Background = (Brush)Application.Current.Resources["CardBackgroundFillColorSecondaryBrush"] };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition());
        grid.RowDefinitions.Add(new RowDefinition());
        var title = new TextBlock { Text = name, TextTrimming = TextTrimming.CharacterEllipsis, Style = (Style)Application.Current.Resources["BodyStrongTextBlockStyle"] };
        ToolTipService.SetToolTip(title, name);
        var info = new TextBlock { Text = $"{p.Num("length_mm"):0.#} mm long · {thick:0.##} mm thick", Style = (Style)Application.Current.Resources["Secondary"] };
        if (p.Bool("too_thin")) info.Foreground = (Brush)Application.Current.Resources["SystemFillColorCautionBrush"];
        var stack = new StackPanel();
        stack.Children.Add(title);
        stack.Children.Add(info);
        grid.Children.Add(stack);
        var action = new ComboBox { MinWidth = 110 };
        Fill(action, ("auto", "Auto"), ("thicken", "Thicken to"), ("remove", "Remove"));
        var mm = new NumberBox { Value = Math.Round(Math.Max(min * 1.5, thick), 1), Minimum = 0.2, Maximum = 20, SmallChange = 0.1,
                                 SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact, IsEnabled = false, Margin = new Thickness(0, 6, 0, 0) };
        action.SelectionChanged += (_, _) => mm.IsEnabled = Picked(action) == "thicken";
        Grid.SetColumn(action, 1);
        grid.Children.Add(action);
        Grid.SetRow(mm, 1);
        Grid.SetColumnSpan(mm, 2);
        grid.Children.Add(mm);
        ThinRows.Children.Add(grid);
        _thin.Add(new ThinRow(name, action, mm));
    }

    // ---------- model export ----------

    async void Export_Click(object sender, RoutedEventArgs e)
    {
        var s = _state!;
        var t = s.Selected;
        if (t == null || ExportFormatBox.SelectedItem is not ComboBoxItem { Tag: string fmt }) return;
        ExportButton.IsEnabled = false;
        ExportResult.IsOpen = false;
        ExportBusy.Visibility = Visibility.Visible;
        try
        {
            var job = await s.Api.StartJobAsync("api/export", Request(t, new JsonObject
            {
                ["format"] = fmt, ["textures"] = TexturesBox.IsChecked == true,
            }));
            var r = await s.Jobs.RunAsync(job);
            var n = r.Items("files").Count();
            ShowResult(ExportResult, InfoBarSeverity.Success, "Exported",
                n > 1 ? $"{n} files, zipped together" : "1 file", [r.Str("download")]);
        }
        catch (Exception ex)
        {
            ShowResult(ExportResult, InfoBarSeverity.Error, "The export failed", LastLines(ex.Message), []);
        }
        finally
        {
            ExportBusy.Visibility = Visibility.Collapsed;
            ExportButton.IsEnabled = true;
        }
    }
}
