using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Meshyard.Services;

namespace Meshyard.Views;

public sealed partial class Inspector : UserControl
{
    AppState? _state;
    MainWindow? _main;
    bool _defaultsApplied;
    int _loadSeq;
    string _variant = "preview";
    bool _settingGear;
    bool _syncingSize;
    double[]? _realM;                // the selected model's real size in meters (length, width, height)
    string _sizeBy = Prefs.Get("printSizeBy", "size");  // which field the user set last: "scale" or "size"
    readonly List<ThinRow> _thin = [];

    sealed record ThinRow(string Name, ComboBox Action, NumberBox Mm);

    public Inspector()
    {
        InitializeComponent();
        // A number box's up/down buttons stay open while it has focus, and clicking the
        // panel's empty space doesn't take focus, so they couldn't be closed: take it here.
        Details.AddHandler(PointerPressedEvent, new Microsoft.UI.Xaml.Input.PointerEventHandler(Panel_Pressed), true);
    }

    void Panel_Pressed(object sender, Microsoft.UI.Xaml.Input.PointerRoutedEventArgs e)
    {
        for (var o = e.OriginalSource as DependencyObject; o != null && o != Details; o = VisualTreeHelper.GetParent(o))
            if (o is NumberBox or Control { IsTabStop: true }) return;  // a control that takes focus itself
        if (Microsoft.UI.Xaml.Input.FocusManager.GetFocusedElement(XamlRoot) is DependencyObject f && InNumberBox(f))
            Details.Focus(FocusState.Pointer);
    }

    static bool InNumberBox(DependencyObject o)
    {
        for (; o != null; o = VisualTreeHelper.GetParent(o))
            if (o is NumberBox) return true;
        return false;
    }

    public void Init(AppState state, MainWindow main)
    {
        _state = state;
        _main = main;
        Fill(ModeBox, ("solid", "Solid, watertight"), ("parts", "Separate parts (kit)"));
        Fill(FormatBox, ("3mf", "3MF"), ("stl", "STL"), ("obj", "OBJ"), ("ply", "PLY"));
        Fill(ColorsBox, ("0", "Off"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6"), ("8", "8"), ("12", "12"), ("16", "16"));
        Fill(StandBox, ("none", "No stand"), ("peg", "Separate stand, pegs into a socket"),
             ("attached", "Stand printed with the model"));
        Fill(ShapeBox, ("round", "Round"), ("rect", "Rectangle with name plate"));
        Fill(LogoBox, ("auto", "The manufacturer's logo"), ("", "No logo"));
        Fill(PostsBox, ("1", "1"), ("2", "2 (fore and aft)"));
        foreach (var (box, unset) in new[] { (BaseColorBox, "Filament 1"), (LogoColorBox, "Same as base"),
                                             (TextColorBox, "Same as base"), (Text2ColorBox, "Same as base") })
            Fill(box, new[] { ("", unset) }.Concat(Filaments).ToArray());
        state.SelectionChanged += OnSelection;
        state.InstallsLoaded += ApplyDefaults;
        state.InstallsLoaded += () => _ = LoadLogos();
    }

    // the stand's filament choices (painted 3MF): Bambu Lab PLA Basic colors
    static readonly (string Id, string Label)[] Filaments =
    [
        ("#FFFFFF", "Jade White"), ("#D1D3D5", "Light Gray"), ("#A6A9AA", "Silver"), ("#8E9089", "Gray"),
        ("#545454", "Dark Gray"), ("#000000", "Black"), ("#5B6579", "Blue Gray"), ("#F7E6DE", "Beige"),
        ("#E4BD68", "Gold"), ("#847D48", "Bronze"), ("#9D432C", "Brown"), ("#6F5034", "Cocoa Brown"),
        ("#C12E1F", "Red"), ("#9D2235", "Maroon Red"), ("#EC008C", "Magenta"), ("#F55A74", "Pink"),
        ("#FF6A13", "Orange"), ("#FF9016", "Pumpkin Orange"), ("#F4EE2A", "Yellow"), ("#FEC600", "Sunflower Yellow"),
        ("#BECF00", "Bright Green"), ("#00AE42", "Bambu Green"), ("#3F8E43", "Mistletoe Green"), ("#00B1B7", "Turquoise"),
        ("#0086D6", "Cyan"), ("#0056B8", "Cobalt Blue"), ("#0A2989", "Blue"), ("#5E43B7", "Purple"), ("#482960", "Indigo Purple"),
    ];

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
        _syncingSize = true;
        SizeBox.Value = double.TryParse(Prefs.Get("printSize"), out var mm) ? mm : p.Num("size_mm", 150);
        ScaleBox.Value = double.TryParse(Prefs.Get("printScale"), out var n) ? n : double.NaN;
        _syncingSize = false;
        VoxelBox.Value = p.Num("voxel_mm", 0.2);
        NozzleBox.Value = p.Num("nozzle_mm", 0.4);
        MinNBox.Value = p.Num("min_feature_nozzles", 3);
        StandHBox.Value = p.Num("stand_height_mm", 30);
        PegFwdBox.Value = 0;
        PegSideBox.Value = 0;
        TiltBox.Value = p.Num("tilt_deg", 0);
        RollBox.Value = p.Num("roll_deg", 0);
        HollowBox.Value = p.Num("hollow_mm", 0);
        ThickenSwitch.IsOn = p.Bool("thicken", true);
        Pick(ModeBox, p.Str("mode", "solid"));
        Pick(FormatBox, p.Str("format", "3mf"));
        Pick(ColorsBox, $"{p.Long("paint_colors")}");
        Pick(StandBox, p.Str("stand", "none"));
        Pick(ShapeBox, p.Str("stand_shape", "round"));
        Pick(PostsBox, $"{Math.Max(1, p.Long("stand_posts"))}");
        BaseWBox.Value = p.Num("stand_base_w_mm", 0);
        BaseDBox.Value = p.Num("stand_base_d_mm", 0);
        Pick(BaseColorBox, p.Str("stand_base_color", ""));
        Pick(LogoColorBox, p.Str("stand_logo_color", ""));
        Pick(TextColorBox, p.Str("stand_text_color", ""));
        Pick(Text2ColorBox, p.Str("stand_text2_color", ""));
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
        VtolRow.Visibility = GearRow.Visibility;
        FlightRow.Visibility = GearRow.Visibility;
        PrintResult.IsOpen = false;
        ExportResult.IsOpen = false;
        PlateText.Text = t.Name;
        _ = ShowLastPrint(t);
        if (t.IsFigure && Picked(LogoBox) == "auto") Pick(LogoBox, "");  // a figure has no maker of its own
        ClearThin();
        SizeBox.Header = t.IsFigure ? "Height (mm)" : "Longest side (mm)";
        _realM = null;
        UpdateScaleInfo();
        if (t.IsFigure)
        {
            // figures: size is the height; finer detail and a stand under the feet suit them
            if (_sizeBy == "size" && SizeBox.Value > 120)
            {
                _syncingSize = true;  // a figure-sized default, not the user's own choice
                SizeBox.Value = 75;
                _syncingSize = false;
            }
            if (VoxelBox.Value > 0.12) VoxelBox.Value = 0.1;
            if (Picked(StandBox) == "none") Pick(StandBox, "attached");
            _loadSeq++;
            _ = _state!.Viewer.Show(_state.Api.Url(t.FigureUrl!));
            _ = Measure(t, "preview");
        }
        else
        {
            _ = LoadPaints(t);
            _ = LoadModel("preview");
        }
    }

    // ---------- paints ----------

    bool _settingPaint;

    /// <summary>The ship's paints (liveries), its default first; hidden when it has none.</summary>
    async Task LoadPaints(Target t)
    {
        _settingPaint = true;
        PaintBox.Items.Clear();
        PaintBox.Visibility = Visibility.Collapsed;
        _settingPaint = false;
        if (t.Asset == null) return;
        try
        {
            var d = await _state!.Api.GetAsync($"api/assets/{t.Asset.Id}/paints", ("channel", _state.Channel));
            if (_state.Selected != t) return;
            var items = d["items"] as JsonArray ?? [];
            if (items.Count == 0) return;
            _settingPaint = true;
            foreach (var p in items)
                PaintBox.Items.Add(new ComboBoxItem { Content = p.Str("name"), Tag = p.Str("id") });
            PaintBox.SelectedIndex = 0;
            _settingPaint = false;
            PaintBox.Visibility = Visibility.Visible;
        }
        catch { /* no paints: the box stays hidden */ }
    }

    string PaintId => PaintBox.Visibility == Visibility.Visible && PaintBox.SelectedItem is ComboBoxItem { Tag: string id } ? id : "";

    void Paint_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_settingPaint || _state?.Selected?.Asset == null) return;
        _ = LoadModel(_variant);
    }

    bool GearUp => GearSwitch.IsOn && GearRow.Visibility == Visibility.Visible && _state?.Selected?.Asset != null;
    bool WeaponsOut => WeaponsSwitch.IsOn && WeaponsRow.Visibility == Visibility.Visible && _state?.Selected?.Asset != null;
    bool VtolOn => VtolSwitch.IsOn && VtolRow.Visibility == Visibility.Visible && _state?.Selected?.Asset != null;
    bool FlightOn => FlightSwitch.IsOn && FlightRow.Visibility == Visibility.Visible && _state?.Selected?.Asset != null;

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
        var vtol = VtolOn && variant != "animated";
        var flight = FlightOn && variant != "animated";
        if (!spawn)
        {
            _variant = variant;
            await s.Viewer.Clear(gearUp || weaponsOut || vtol || flight
                ? (gearUp ? "Retracting the landing gear" : weaponsOut ? "Deploying the weapons"
                   : vtol ? "Swinging the thrusters into VTOL mode" : "Opening the flaps")
                  + "… the first time for each ship takes a little longer."
                : "Exporting the model… big ships can take a minute or two the first time.");
        }
        try
        {
            var job = await s.Api.StartJobAsync("api/model", new JsonObject
            {
                ["channel"] = s.Channel, ["id"] = t.Asset.Id, ["variant"] = variant, ["spawn"] = spawn,
                ["gear"] = gearUp ? "up" : "down", ["weapons"] = weaponsOut ? "out" : "", ["vtol"] = vtol, ["flight"] = flight,
                ["paint"] = variant == "animated" ? "" : PaintId,
            });
            var r = await s.Jobs.RunAsync(job);
            if (!spawn && seq == _loadSeq && r.Str("url") is { Length: > 0 } url)
            {
                await s.Viewer.Show(s.Api.Url(url));
                _ = Measure(t, variant);
                RefreshPeg(0);
            }
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
            if (vtol && e.Message.Contains("VTOL", StringComparison.Ordinal))
            {
                // most ships have no VTOL mode: say so, and show it as exported
                _main!.ShowError("This ship has no VTOL mode", LastLines(e.Message));
                _settingGear = true;
                VtolSwitch.IsOn = false;
                _settingGear = false;
                if (!spawn && seq == _loadSeq) _ = LoadModel(variant);
                return;
            }
            if (flight && e.Message.Contains("flight mode", StringComparison.Ordinal))
            {
                // only a few ships have flaps that open for flight: say so, and show it as exported
                _main!.ShowError("This ship has no flight mode flaps", LastLines(e.Message));
                _settingGear = true;
                FlightSwitch.IsOn = false;
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

    // Scale and size are two views of one number: with the model's real size known,
    // setting either one fills in the other. Whichever the user set last is what the
    // export uses, and it carries over to the next model, so a fleet printed at 1:500
    // stays at 1:500.

    /// <summary>The model's real size in meters, from the file the viewer just loaded.</summary>
    async Task Measure(Target t, string variant)
    {
        var s = _state!;
        try
        {
            var job = await s.Api.StartJobAsync("api/print/measure", Request(t, new JsonObject { ["variant"] = variant }));
            var r = await s.Jobs.RunAsync(job, quiet: true);
            if (s.Selected != t) return;
            var dims = (r["real_m"] as JsonArray ?? []).Select(x => x!.GetValue<double>()).ToArray();
            if (dims.Length != 3 || dims.Max() <= 0) return;
            _realM = dims;
            SyncSize();
        }
        catch { /* the fields still work, just without the conversion */ }
    }

    double LongestM => _realM?.Max() ?? 0;

    void Scale_Changed(NumberBox sender, NumberBoxValueChangedEventArgs args)
    {
        if (_syncingSize) return;
        if (!double.IsNaN(ScaleBox.Value)) SetSizeBy("scale");
        SyncSize();
        RefreshPeg();  // the peg's size and the offsets' reach follow the print size
    }

    void Size_Changed(NumberBox sender, NumberBoxValueChangedEventArgs args)
    {
        if (_syncingSize) return;
        SetSizeBy("size");
        SyncSize();
        RefreshPeg();
    }

    void SetSizeBy(string by)
    {
        _sizeBy = by;
        Prefs.Set("printSizeBy", by);
        Prefs.Set(by == "scale" ? "printScale" : "printSize", $"{(by == "scale" ? ScaleBox.Value : SizeBox.Value)}");
    }

    /// <summary>Fill in whichever field the user didn't set.</summary>
    void SyncSize()
    {
        if (LongestM > 0)
        {
            _syncingSize = true;
            if (_sizeBy == "scale" && Safe(ScaleBox.Value) > 0)
                SizeBox.Value = Math.Round(LongestM * 1000 / ScaleBox.Value, 1);
            else if (Safe(SizeBox.Value) > 0)
                ScaleBox.Value = Math.Round(LongestM * 1000 / SizeBox.Value, LongestM * 1000 / SizeBox.Value < 10 ? 1 : 0);
            _syncingSize = false;
        }
        UpdateScaleInfo();
    }

    void UpdateScaleInfo()
    {
        if (_state?.Selected == null || _realM == null) { ScaleInfo.Text = ""; return; }  // not measured yet
        var real = string.Join(" × ", _realM.Select(m => $"{m:0.#}"));
        var n = Safe(ScaleBox.Value);
        ScaleInfo.Text = n > 0
            ? $"Real size {real} m. At 1:{n:0.#} it prints {string.Join(" × ", _realM.Select(m => $"{m * 1000 / n:0}"))} mm."
            : $"Real size {real} m.";
    }

    void UpdateStrength()
    {
        var mm = Safe(NozzleBox.Value) * Safe(MinNBox.Value);
        MinMmText.Text = $"Parts thinner than {mm:0.##} mm are thickened.";
    }

    static double Safe(double v) => double.IsNaN(v) ? 0 : v;

    void Stand_Changed(object sender, SelectionChangedEventArgs e) { UpdateStand(); RefreshPeg(); }

    void Peg_Changed(NumberBox sender, NumberBoxValueChangedEventArgs args) => RefreshPeg();

    void StandView_Toggled(object sender, RoutedEventArgs e)
    {
        if (_state == null) return;
        _ = _state.Viewer.StandView(StandViewSwitch.IsOn);
        // ask for the stand again so the view never shows a stale or missing one
        if (StandViewSwitch.IsOn) RefreshPeg(0);
    }

    void RefreshStand_Click(object sender, RoutedEventArgs e) => RefreshPeg(0);

    // Where the stand's peg will go with the current settings, drawn in the viewer so the
    // Peg forward / Peg right offsets can be set before exporting. Asked again (after a
    // short pause) whenever a setting that moves it changes.
    CancellationTokenSource? _pegCts;
    int _pegSeq;

    // how the ship sits on the stand, in words (the stand view shows it from the front)
    static string StandLine(JsonNode r)
    {
        double inc = r["incline"]?.GetValue<double>() ?? 0, tilt = r["tilt"]?.GetValue<double>() ?? 0;
        string pitch = inc == 0 ? "level" : $"nose {(inc > 0 ? "up" : "down")} {Math.Abs(inc):0}°";
        string bank = tilt == 0 ? "wings level"
            : $"{(tilt > 0 ? "right wing down (toward you)" : "left wing down (away from you)")} {Math.Abs(tilt):0}°";
        var stand = r["stand_url"] != null
            ? "The stand shown is the one the export builds, at preview detail."
            : "The stand in the viewer is a simple stand-in: the exported stand adds the name plate, lettering, logo and beveled front.";
        var balance = r["balance"]?.GetValue<double>() is double b
            ? b < 0 ? $" Warning: the ship's balance point is {-b:0} mm outside the base, so it would tip over. Make the base bigger, add a second post or move the peg."
                    : $" Balance point {b:0} mm inside the base edge."
            : "";
        return $"Seen from the stand's front (the light edge): nose to the right, {pitch}, {bank}. " + stand + balance;
    }

    async void RefreshPeg(int delayMs = 600)
    {
        var s = _state;
        var t = s?.Selected;
        _pegCts?.Cancel();
        var cts = _pegCts = new CancellationTokenSource();
        var seq = ++_pegSeq;
        try { await Task.Delay(delayMs, cts.Token); } catch (TaskCanceledException) { return; }
        if (s == null || t == null || seq != _pegSeq) return;
        if (Picked(StandBox) == "none")
        {
            PegNote.Text = "";
            await s.Viewer.Peg(null);
            return;
        }
        PegNote.Text = "Finding where the peg goes…";
        try
        {
            var job = await s.Api.StartJobAsync("api/print/peg",
                Request(t, new JsonObject { ["variant"] = _variant, ["options"] = PrintOptions() }));
            var r = await s.Jobs.RunAsync(job, quiet: true);
            if (seq != _pegSeq || s.Selected != t) return;
            var shown = r["top"] != null;
            await s.Viewer.Peg(shown ? r : null);
            PegNote.Text = shown ? "The peg is shown in orange in the viewer. " + StandLine(r) : "";
        }
        catch (Exception ex)
        {
            // say so: a silent failure left "Show on stand" doing nothing
            if (seq == _pegSeq) PegNote.Text = "Couldn't build the stand preview: " + LastLines(ex.Message);
        }
    }

    void UpdateStand()
    {
        var none = Picked(StandBox) == "none";
        ShapeBox.IsEnabled = !none;
        StandHBox.IsEnabled = !none;
        PegFwdBox.IsEnabled = !none;
        PegSideBox.IsEnabled = !none;
        PlateText.IsEnabled = PlateText2.IsEnabled = LogoBox.IsEnabled = ScaleSwitch.IsEnabled = !none && Picked(ShapeBox) == "rect";
        PostsBox.IsEnabled = BaseWBox.IsEnabled = RefreshStandButton.IsEnabled = !none;
        BaseDBox.IsEnabled = !none && Picked(ShapeBox) == "rect";
        // the stand's colors go into a painted 3MF only
        var painted = Picked(FormatBox) == "3mf" && Picked(ColorsBox) is var c && c != "" && c != "0";
        BaseColorBox.IsEnabled = LogoColorBox.IsEnabled = TextColorBox.IsEnabled = Text2ColorBox.IsEnabled = !none && painted;
    }

    void Paint_Toggled(object sender, SelectionChangedEventArgs e) => UpdateStand();

    void StandSetting_Changed(object sender, SelectionChangedEventArgs e) => RefreshPeg();
    // the viewer's stand shows the plate's lettering: rebuild it once typing pauses
    void Plate_Changed(object sender, TextChangedEventArgs e) => RefreshPeg(1000);
    void Plate_Toggled(object sender, RoutedEventArgs e) => RefreshPeg();

    JsonObject PrintOptions()
    {
        var o = new JsonObject
        {
            ["mode"] = Picked(ModeBox), ["format"] = Picked(FormatBox), ["stand"] = Picked(StandBox),
            ["thicken"] = ThickenSwitch.IsOn, ["stand_shape"] = Picked(ShapeBox), ["stand_text"] = PlateText.Text.Trim(),
            ["stand_text2"] = PlateText2.Text.Trim(), ["stand_logo"] = Picked(LogoBox), ["stand_scale"] = ScaleSwitch.IsOn,
            ["size_mm"] = Safe(SizeBox.Value), ["voxel_mm"] = Safe(VoxelBox.Value), ["nozzle_mm"] = Safe(NozzleBox.Value),
            ["min_feature_nozzles"] = Safe(MinNBox.Value), ["stand_height_mm"] = Safe(StandHBox.Value),
            ["stand_forward_mm"] = Safe(PegFwdBox.Value), ["stand_side_mm"] = Safe(PegSideBox.Value),
            ["tilt_deg"] = Safe(TiltBox.Value), ["roll_deg"] = Safe(RollBox.Value), ["hollow_mm"] = Safe(HollowBox.Value),
            ["paint_colors"] = Picked(FormatBox) == "3mf" && int.TryParse(Picked(ColorsBox), out var nc) ? nc : 0,
            ["stand_posts"] = int.TryParse(Picked(PostsBox), out var np) ? np : 1,
            ["stand_base_w_mm"] = Safe(BaseWBox.Value), ["stand_base_d_mm"] = Safe(BaseDBox.Value),
            ["stand_base_color"] = Picked(BaseColorBox), ["stand_logo_color"] = Picked(LogoColorBox),
            ["stand_text_color"] = Picked(TextColorBox), ["stand_text2_color"] = Picked(Text2ColorBox),
        };
        // a scale the user chose wins, so every model comes out at that scale
        if (_sizeBy == "scale" && Safe(ScaleBox.Value) > 0) o["scale"] = ScaleBox.Value;
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
        if (VtolOn) body["vtol"] = true;
        if (FlightOn) body["flight"] = true;
        if (PaintId is { Length: > 0 } paint) body["paint"] = paint;
        return body;
    }

    // ---------- print log ----------

    static string LogKey(Target t) => t.IsFigure ? "figure:" + t.Name : t.Asset?.Record ?? "";

    /// <summary>"Last printed ..." for the selected item, from the print log.</summary>
    async Task ShowLastPrint(Target t)
    {
        LastPrintText.Visibility = Visibility.Collapsed;
        try
        {
            var d = await _state!.Api.GetAsync("api/print/log", ("key", LogKey(t)));
            if (_state.Selected != t) return;
            var rows = d.Items("items").ToList();
            if (rows.Count == 0) return;
            var r = rows[0];
            var more = rows.Count > 1 ? $" ({rows.Count} prints in all)" : "";
            LastPrintText.Text = $"Last printed {r.Str("date")} at {r.Str("scale")}, {r.Str("longest_side_mm")} mm{more}.";
            LastPrintText.Visibility = Visibility.Visible;
        }
        catch { /* no log yet */ }
    }

    async void History_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var d = await _state!.Api.GetAsync("api/print/log");
            var rows = d.Items("items").ToList();
            var list = new StackPanel { Spacing = 8 };
            if (rows.Count == 0)
                list.Children.Add(new TextBlock { Text = "Nothing exported for printing yet.", Style = (Style)Application.Current.Resources["Secondary"] });
            foreach (var r in rows.Take(300))
            {
                var row = new StackPanel();
                row.Children.Add(new TextBlock { Text = $"{r.Str("name")}  ·  {r.Str("scale")}", Style = (Style)Application.Current.Resources["BodyStrongTextBlockStyle"] });
                var bits = new List<string> { r.Str("date"), $"{r.Str("longest_side_mm")} mm" };
                if (r.Str("stand").Length > 0) bits.Add($"{r.Str("stand")} stand");
                if (r.Str("plate").Length > 0) bits.Add($"plate: {r.Str("plate")}");
                row.Children.Add(new TextBlock { Text = string.Join("  ·  ", bits), TextWrapping = TextWrapping.Wrap,
                                                 Style = (Style)Application.Current.Resources["Secondary"] });
                list.Children.Add(row);
            }
            var dialog = new ContentDialog
            {
                XamlRoot = XamlRoot,
                Title = "Print history",
                Content = new ScrollViewer { Content = list, MaxHeight = 480 },
                PrimaryButtonText = rows.Count > 0 ? "Open the log file" : "",
                CloseButtonText = "Close",
            };
            if (await dialog.ShowAsync() == ContentDialogResult.Primary)
                await Windows.System.Launcher.LaunchUriAsync(new Uri(d.Str("path")));
        }
        catch (Exception ex) { _main!.ShowError("Couldn't read the print history", ex.Message); }
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
            var colors = r.Items("colors").Select(c => c.GetValue<string>()).ToList();
            if (colors.Count > 0)  // painted 3MF: which color goes in which filament slot
            {
                // the closest Bambu PLA Basic color names the spool to load in each slot
                var names = r.Items("color_names").Select(c => c.GetValue<string>()).ToList();
                parts.Add("painted for Bambu Studio (import it into a project with Ctrl+I to match the colors to your filaments), colors " + string.Join(", ", colors.Select((c, i) =>
                    i < names.Count ? $"{i + 1} {c} ({names[i]})" : $"{i + 1} {c}")));
            }
            var files = r.Items("files").Select(f => f.GetValue<string>()).ToList();
            ShowResult(PrintResult, InfoBarSeverity.Success, $"{size} mm ({r.Str("scale")})", string.Join(" · ", parts), files);
            _ = ShowLastPrint(t);
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
