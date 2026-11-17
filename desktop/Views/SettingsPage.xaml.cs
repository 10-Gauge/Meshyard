using System.Reflection;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Meshyard.Services;

namespace Meshyard.Views;

public sealed partial class SettingsPage : UserControl
{
    AppState? _state;
    MainWindow? _main;
    bool _init;

    public SettingsPage() => InitializeComponent();

    public void Init(AppState state, MainWindow main)
    {
        _state = state;
        _main = main;
        var themes = new List<Choice> { new("system", "Use Windows setting"), new("light", "Light"), new("dark", "Dark") };
        ThemeBox.ItemsSource = themes;
        ThemeBox.SelectedItem = themes.FirstOrDefault(t => t.Id == Prefs.Get("theme", "system")) ?? themes[0];
        HomeText.Text = Paths.Home;
        AboutTitle.Text = "Meshyard " + (Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "");
        _init = true;
    }

    public void Activate() => Refresh();

    public void Refresh()
    {
        if (_state == null) return;
        InstallRows.Children.Clear();
        if (_state.Installs.Count == 0)
            InstallRows.Children.Add(new TextBlock { Text = "None found yet. Click Find data files to look for some.", Style = Res<Style>("Secondary") });
        foreach (var i in _state.Installs)
        {
            var status = i.Indexed ? "Indexed" : i.IndexState switch
            {
                "running" => "Indexing…",
                "error" => "Indexing failed",
                _ => "Not indexed",
            };
            InstallRows.Children.Add(Row(i.Indexed ? "" : "", i.Indexed, $"{i.Channel} {i.Version}",
                $"{i.Path} · build {i.Change}", status));
        }
        ToolRows.Children.Clear();
        foreach (var (key, label) in new[] { ("starbreaker", "StarBreaker"), ("cgf_converter", "cgf-converter"), ("blender", "Blender") })
        {
            var path = _state.Tools.GetValueOrDefault(key);
            ToolRows.Children.Add(Row(path != null ? "" : "", path != null, label,
                path ?? (key == "blender" ? "Not installed (optional, for the sandbox)" : "Not downloaded"), null));
        }
    }

    static T Res<T>(string key) => (T)Application.Current.Resources[key];

    static Grid Row(string glyph, bool ok, string title, string sub, string? right)
    {
        var g = new Grid { ColumnSpacing = 12, Padding = new Thickness(12, 10, 12, 10), CornerRadius = new CornerRadius(4),
                           Background = Res<Brush>("CardBackgroundFillColorSecondaryBrush") };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var icon = new FontIcon { Glyph = glyph, FontSize = 16, VerticalAlignment = VerticalAlignment.Center,
                                  Foreground = Res<Brush>(ok ? "SystemFillColorSuccessBrush" : "SystemFillColorCautionBrush") };
        var text = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
        text.Children.Add(new TextBlock { Text = title, Style = Res<Style>("BodyStrongTextBlockStyle") });
        text.Children.Add(new TextBlock { Text = sub, Style = Res<Style>("Secondary"), TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.NoWrap });
        Grid.SetColumn(text, 1);
        g.Children.Add(icon);
        g.Children.Add(text);
        if (right != null)
        {
            var r = new TextBlock { Text = right, Style = Res<Style>("Secondary"), VerticalAlignment = VerticalAlignment.Center };
            Grid.SetColumn(r, 2);
            g.Children.Add(r);
        }
        return g;
    }

    void Theme_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (!_init || ThemeBox.SelectedItem is not Choice c) return;
        Prefs.Set("theme", c.Id);
        _main!.ApplyTheme(c.Id);
    }

    async void AddFolder_Click(object sender, RoutedEventArgs e) => await _main!.FindDataFilesAsync();

    async void Tools_Click(object sender, RoutedEventArgs e)
    {
        ToolsButton.IsEnabled = false;
        await _main!.DownloadTools();
        ToolsButton.IsEnabled = true;
        Refresh();
    }

    void Exports_Click(object sender, RoutedEventArgs e)
    {
        Directory.CreateDirectory(Paths.Exports);
        Paths.Reveal(Paths.Exports);
    }

    void Home_Click(object sender, RoutedEventArgs e) => Paths.Reveal(Paths.Home);
    void Log_Click(object sender, RoutedEventArgs e) => Paths.OpenFile(Path.Combine(Paths.Home, "engine.log"));
}
