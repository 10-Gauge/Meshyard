using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using SCAssetBrowser.Services;

namespace SCAssetBrowser.Views;

public sealed partial class CharacterPanel : UserControl
{
    AppState? _state;
    MainWindow? _main;
    string? _loadedBody;
    string? _loadedChannel;
    readonly Dictionary<string, Choice?> _picks = [];
    readonly List<Picker> _slots = [];
    Picker? _weapon;

    public CharacterPanel() => InitializeComponent();

    public void Init(AppState state, MainWindow main)
    {
        _state = state;
        _main = main;
        BodyBox.ItemsSource = new List<Choice> { new("male", "Male"), new("female", "Female") };
        BodyBox.SelectedIndex = 0;
        _weapon = new Picker(WeaponBox, q => Fetch("api/character/weapons", ("q", q)));
        state.ChannelChanged += () => { if (Visibility == Visibility.Visible) _ = Load(); else _loadedChannel = null; };
    }

    string Body => (BodyBox.SelectedItem as Choice)?.Id ?? "male";

    /// <summary>Called when the Characters page is shown.</summary>
    public void Activate()
    {
        if (_loadedBody != Body || _loadedChannel != _state?.Channel) _ = Load();
    }

    async Task<List<Choice>> Fetch(string path, params (string, object?)[] q)
    {
        var s = _state!;
        var all = new List<(string, object?)> { ("channel", s.Channel), ("body", Body) };
        all.AddRange(q);
        var d = await s.Api.GetAsync(path, all.ToArray());
        return d.Items("items").Select(i => new Choice(i["id"]!.ToString(), i.Str("name"))).ToList();
    }

    async Task Load()
    {
        var s = _state!;
        if (s.Channel == null || s.CurrentInstall is not { Indexed: true }) return;
        SetBusy("Loading…");
        try
        {
            var d = await s.Api.GetAsync("api/character/slots", ("channel", s.Channel), ("body", Body));
            _loadedBody = Body;
            _loadedChannel = s.Channel;

            var heads = new List<Choice> { new("", "(none)") };
            heads.AddRange(d.Items("heads").Select(h => new Choice(h.Str("id"), h.Str("name"))));
            HeadBox.ItemsSource = heads;
            HeadBox.SelectedIndex = heads.Count > 1 ? 1 : 0;

            if (PoseBox.ItemsSource == null)
            {
                var poses = new List<Choice> { new("", "A-pose (as exported, no weapon)") };
                poses.AddRange(d.Items("poses").Select(p => new Choice(p.Str("id"), p.Str("label"))));
                PoseBox.ItemsSource = poses;
                PoseBox.SelectedItem = poses.FirstOrDefault(p => p.Id == "relaxed") ?? poses[0];
            }

            Slots.Children.Clear();
            _slots.Clear();
            foreach (var slot in d.Items("slots"))
            {
                var id = slot.Str("id");
                var box = new AutoSuggestBox { Header = slot.Str("label"), PlaceholderText = "(none)", QueryIcon = new SymbolIcon(Symbol.Find) };
                var picker = new Picker(box, q => Fetch("api/character/items", ("slot", id), ("q", q)), id);
                picker.Changed += c => _picks[id] = c;
                if (_picks.GetValueOrDefault(id) is { } keep) picker.Set(keep);
                Slots.Children.Add(box);
                _slots.Add(picker);
            }
            SetBusy(null);
        }
        catch (Exception e) { SetBusy(e.Message, spinner: false); }
    }

    void Body_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_state == null) return;
        _picks.Clear();
        _ = Load();
    }

    void Pose_Changed(object sender, SelectionChangedEventArgs e) =>
        WeaponBox.IsEnabled = (PoseBox.SelectedItem as Choice)?.Id is { Length: > 0 };

    void Clear_Click(object sender, RoutedEventArgs e)
    {
        _picks.Clear();
        foreach (var p in _slots) p.Set(null);
    }

    void SetBusy(string? text, bool spinner = true)
    {
        Busy.Visibility = text == null ? Visibility.Collapsed : Visibility.Visible;
        ((ProgressRing)Busy.Children[0]).Visibility = spinner ? Visibility.Visible : Visibility.Collapsed;
        BusyText.Text = text ?? "";
    }

    string FigureName()
    {
        var names = _slots.Select(p => p.Value?.Label).Where(n => n != null).Cast<string>().ToList();
        var core = names.FirstOrDefault(n => System.Text.RegularExpressions.Regex.IsMatch(n, "core|armor|armour", System.Text.RegularExpressions.RegexOptions.IgnoreCase))
                   ?? names.FirstOrDefault();
        return core == null ? "Character"
            : System.Text.RegularExpressions.Regex.Replace(core, @"\s+(Core|Arms|Legs|Helmet)\b.*$", "", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
    }

    async void Build_Click(object sender, RoutedEventArgs e)
    {
        var s = _state!;
        _weapon!.Commit();  // typed but not yet committed text counts too
        foreach (var picker in _slots) picker.Commit();
        var pose = (PoseBox.SelectedItem as Choice)?.Id ?? "";
        var weapon = pose != "" ? _weapon!.Value : null;
        var picks = new JsonObject();
        foreach (var (k, v) in _picks) if (v != null) picks[k] = long.Parse(v.Id);
        BuildButton.IsEnabled = false;
        SetBusy("Building… the first time each piece is exported takes a few seconds.");
        await s.Viewer.Clear("Assembling the character…");
        try
        {
            var job = await s.Api.StartJobAsync("api/character/build", new JsonObject
            {
                ["channel"] = s.Channel, ["body"] = Body, ["picks"] = picks,
                ["head"] = (HeadBox.SelectedItem as Choice)?.Id ?? "", ["pose"] = pose,
                ["weapon"] = weapon != null ? long.Parse(weapon.Id) : null,
            });
            var r = await s.Jobs.RunAsync(job, line => BusyText.Text = line.Trim());
            var parts = _slots.Select(p => p.Value?.Label).Where(n => n != null).Cast<string>().ToList();
            if (pose != "") parts.Add(((Choice)PoseBox.SelectedItem).Label);
            if (weapon != null) parts.Add(weapon.Label);
            _main!.ClearBrowseSelection();
            s.Select(new Target
            {
                Figure = r.Str("figure"), FigureUrl = r.Str("url"), Name = FigureName(),
                Meta = $"{(Body == "male" ? "Male" : "Female")} · {(parts.Count > 0 ? string.Join(" · ", parts) : "no armour")}",
                Description = "Built from your selections. The print settings apply to the figure; its size is its height.",
            });
            SetBusy(null);
        }
        catch (Exception ex)
        {
            SetBusy(string.Join(" ", ex.Message.Split('\n', StringSplitOptions.RemoveEmptyEntries).TakeLast(2)), spinner: false);
            await s.Viewer.Say("The build failed");
        }
        finally { BuildButton.IsEnabled = true; }
    }

    /// <summary>An AutoSuggestBox that searches a list on the engine and holds the chosen item.</summary>
    sealed class Picker
    {
        readonly AutoSuggestBox _box;
        readonly Func<string, Task<List<Choice>>> _fetch;
        CancellationTokenSource? _typing;
        int _seq;

        public Choice? Value { get; private set; }
        public event Action<Choice?>? Changed;

        public Picker(AutoSuggestBox box, Func<string, Task<List<Choice>>> fetch, string? id = null)
        {
            _box = box;
            _fetch = fetch;
            box.TextChanged += OnText;
            box.SuggestionChosen += (_, a) => Choose(a.SelectedItem as Choice);
            box.QuerySubmitted += (_, a) =>
            {
                if (a.ChosenSuggestion is Choice c) Choose(c);
                else if (_box.ItemsSource is List<Choice> { Count: > 0 } list) Choose(list[0]);
            };
            box.GotFocus += async (_, _) =>
            {
                if (_box.ItemsSource == null || Value != null) await Suggest(Value == null ? _box.Text : "");
                _box.IsSuggestionListOpen = true;
            };
            box.LostFocus += (_, _) => Commit();
        }

        /// <summary>When focus leaves, typed text either becomes a real choice or is cleared,
        /// so what the box shows is always what gets built.</summary>
        public void Commit()
        {
            var text = _box.Text.Trim();
            if (Value != null && text == Value.Label) return;
            var list = _box.ItemsSource as List<Choice> ?? [];
            var match = list.FirstOrDefault(c => string.Equals(c.Label, text, StringComparison.OrdinalIgnoreCase))
                        ?? (text.Length > 0 && list.Count == 1 ? list[0] : null);
            if (match != null) Choose(match);
            else if (text.Length == 0 || Value == null)
            {
                _box.Text = "";
                if (Value != null) { Value = null; Changed?.Invoke(null); }
            }
            else _box.Text = Value.Label;
        }

        public void Set(Choice? c)
        {
            Value = c;
            _box.Text = c?.Label ?? "";
        }

        void Choose(Choice? c)
        {
            if (c == null) return;
            Value = c;
            _box.Text = c.Label;
            Changed?.Invoke(c);
        }

        async void OnText(AutoSuggestBox sender, AutoSuggestBoxTextChangedEventArgs args)
        {
            if (args.Reason != AutoSuggestionBoxTextChangeReason.UserInput) return;
            if (sender.Text.Length == 0 && Value != null)
            {
                Value = null;
                Changed?.Invoke(null);
            }
            _typing?.Cancel();
            var cts = _typing = new CancellationTokenSource();
            try { await Task.Delay(220, cts.Token); } catch (TaskCanceledException) { return; }
            await Suggest(sender.Text);
        }

        async Task Suggest(string q)
        {
            var seq = ++_seq;  // only the latest request may fill the list
            try
            {
                var items = await _fetch(q.Trim());
                if (seq == _seq) _box.ItemsSource = items;
            }
            catch { if (seq == _seq) _box.ItemsSource = new List<Choice>(); }
        }
    }
}
