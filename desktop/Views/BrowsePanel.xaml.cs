using System.Collections.ObjectModel;
using System.Runtime.InteropServices.WindowsRuntime;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Data;
using SCAssetBrowser.Services;
using Windows.Foundation;

namespace SCAssetBrowser.Views;

public sealed partial class BrowsePanel : UserControl
{
    AppState? _state;
    MainWindow? _main;
    string _category = "";
    string _query = "";
    CancellationTokenSource? _typing;
    bool _settingCats;
    AssetSource? _source;

    public BrowsePanel() => InitializeComponent();

    public void Init(AppState state, MainWindow main)
    {
        _state = state;
        _main = main;
        state.ChannelChanged += () => { _category = ""; Refresh(); };
    }

    public void Refresh()
    {
        if (_state?.Channel == null) return;
        var src = new AssetSource(this);
        _source = src;
        Results.ItemsSource = src;
        NoResults.Visibility = Visibility.Collapsed;
    }

    /// <summary>One page of search results (the list asks for more as it scrolls).</summary>
    internal async Task<(List<AssetItem> Items, long Total)> FetchPage(int offset)
    {
        var s = _state!;
        try
        {
            var d = await s.Api.GetAsync("api/assets", ("channel", s.Channel), ("q", _query), ("category", _category),
                ("kind", RawGeo.IsChecked == true ? "" : "entity"), ("offset", offset), ("limit", 100));
            var total = d.Long("total");
            if (offset == 0)
            {
                CountText.Text = $"{total:N0} assets";
                UpdateCategories(d.Items("categories").Select(c => (c.Str("category"), c.Long("n"))).ToList());
                NoResults.Visibility = total == 0 ? Visibility.Visible : Visibility.Collapsed;
            }
            return (d.Items("items").Select(AssetItem.From).ToList(), total);
        }
        catch (ApiException e)
        {
            CountText.Text = "";
            if (e.NeedsIndex) _ = s.LoadInstallsAsync();  // the workspace shows the indexing state
            else _main?.ShowError("Search failed", e.Message);
            return ([], 0);
        }
    }

    void UpdateCategories(List<(string Key, long N)> cats)
    {
        var raw = RawGeo.IsChecked == true;
        var keys = new List<string> { "" };
        keys.AddRange(cats.Where(c => raw || c.Key != "geometry").Select(c => c.Key));
        var have = Cats.Items.Select(i => (string)i.Tag).ToList();
        if (!have.SequenceEqual(keys))
        {
            _settingCats = true;
            Cats.Items.Clear();
            foreach (var k in keys) Cats.Items.Add(new SelectorBarItem { Text = Labels.Category(k), Tag = k });
            _settingCats = false;
        }
        var total = cats.Sum(c => c.N);
        foreach (var item in Cats.Items)
        {
            var k = (string)item.Tag;
            var n = k == "" ? total : cats.FirstOrDefault(c => c.Key == k).N;
            ToolTipService.SetToolTip(item, $"{n:N0}");
        }
        _settingCats = true;
        Cats.SelectedItem = Cats.Items.FirstOrDefault(i => (string)i.Tag == _category) ?? Cats.Items.FirstOrDefault();
        _settingCats = false;
    }

    void Cats_SelectionChanged(SelectorBar sender, SelectorBarSelectionChangedEventArgs args)
    {
        if (_settingCats || sender.SelectedItem == null) return;
        var k = (string)sender.SelectedItem.Tag;
        if (k == _category) return;
        _category = k;
        Refresh();
    }

    async void Search_TextChanged(AutoSuggestBox sender, AutoSuggestBoxTextChangedEventArgs args)
    {
        if (args.Reason != AutoSuggestionBoxTextChangeReason.UserInput) return;
        _typing?.Cancel();
        var cts = _typing = new CancellationTokenSource();
        try { await Task.Delay(220, cts.Token); } catch (TaskCanceledException) { return; }
        _query = sender.Text.Trim();
        Refresh();
    }

    void Search_QuerySubmitted(AutoSuggestBox sender, AutoSuggestBoxQuerySubmittedEventArgs args)
    {
        _typing?.Cancel();
        _query = sender.Text.Trim();
        Refresh();
    }

    void RawGeo_Changed(object sender, RoutedEventArgs e) => Refresh();

    void Results_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (Results.SelectedItem is not AssetItem a) return;
        _state!.Select(new Target { Asset = a, Name = a.Name, Meta = a.Subtitle, Description = a.Description });
    }

    /// <summary>Called when a figure is selected elsewhere, so the list doesn't look selected.</summary>
    public void ClearSelection() => Results.SelectedItem = null;
}

/// <summary>Search results that load a page at a time as the list scrolls.</summary>
sealed class AssetSource(BrowsePanel owner) : ObservableCollection<AssetItem>, ISupportIncrementalLoading
{
    long _total = -1;
    bool _busy;

    public bool HasMoreItems => _total < 0 || Count < _total;

    public IAsyncOperation<LoadMoreItemsResult> LoadMoreItemsAsync(uint count) =>
        AsyncInfo.Run(async _ =>
        {
            if (_busy) return new LoadMoreItemsResult { Count = 0 };
            _busy = true;
            try
            {
                var (items, total) = await owner.FetchPage(Count);
                _total = items.Count == 0 ? Count : total;
                foreach (var i in items) Add(i);
                return new LoadMoreItemsResult { Count = (uint)items.Count };
            }
            finally { _busy = false; }
        });
}
