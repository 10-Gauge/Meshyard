using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using SCAssetBrowser.Services;

namespace SCAssetBrowser.Views;

public sealed partial class BlenderPage : UserControl
{
    AppState? _state;
    MainWindow? _main;

    public BlenderPage() => InitializeComponent();

    public void Init(AppState state, MainWindow main)
    {
        _state = state;
        _main = main;
    }

    public void Activate()
    {
        Refresh();
        _ = LoadQueue();
    }

    public void Refresh()
    {
        if (_state == null) return;
        var path = _state.Tools.GetValueOrDefault("blender");
        var have = path != null;
        StatusTitle.Text = have ? "Blender is installed" : "Blender isn't installed";
        StatusText.Text = have ? path! : "Install Blender, then come back here. The sandbox needs Blender 4.2 or newer.";
        StatusIcon.Foreground = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources[
            have ? "SystemFillColorSuccessBrush" : "SystemFillColorCautionBrush"];
        GetBlender.Visibility = have ? Visibility.Collapsed : Visibility.Visible;
        InstallButton.IsEnabled = OpenButton.IsEnabled = have;
    }

    async Task LoadQueue()
    {
        try
        {
            var d = await _state!.Api.GetAsync("api/sandbox/queue", ("after", 0));
            var items = d.Items("items").Reverse().Take(50).Select(i =>
            {
                var when = DateTimeOffset.FromUnixTimeSeconds((long)i.Num("added_at")).ToLocalTime();
                var row = new Grid { Padding = new Thickness(0, 6, 0, 6) };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                var left = new StackPanel();
                left.Children.Add(new TextBlock { Text = i.Str("name"), Style = (Style)Application.Current.Resources["BodyStrongTextBlockStyle"] });
                left.Children.Add(new TextBlock { Text = $"{i.Str("channel")} · {i.Str("record")}", Style = (Style)Application.Current.Resources["Secondary"] });
                var right = new TextBlock { Text = when.ToString("g"), Style = (Style)Application.Current.Resources["Secondary"], VerticalAlignment = VerticalAlignment.Center };
                Grid.SetColumn(right, 1);
                row.Children.Add(left);
                row.Children.Add(right);
                return row;
            }).ToList();
            Queue.ItemsSource = items;
            QueueEmpty.Visibility = items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        }
        catch (Exception e) { _main!.ShowError("Couldn't read the spawn queue", e.Message); }
    }

    async Task RunBlender(string action)
    {
        try
        {
            var job = await _state!.Api.StartJobAsync("api/blender", new JsonObject { ["action"] = action });
            await _state.Jobs.RunAsync(job);
        }
        catch (Exception e) when (e is not JobFailedException) { _main!.ShowError("Blender", e.Message); }
        catch (JobFailedException) { }
    }

    void Install_Click(object sender, RoutedEventArgs e) => _ = RunBlender("install");
    void Open_Click(object sender, RoutedEventArgs e) => _ = RunBlender("open");
    void Refresh_Click(object sender, RoutedEventArgs e) => _ = LoadQueue();

    async void Clear_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await _state!.Api.PostAsync("api/sandbox/clear", []);
            await LoadQueue();
        }
        catch (Exception ex) { _main!.ShowError("Couldn't clear the queue", ex.Message); }
    }
}
