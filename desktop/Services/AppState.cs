using System.Text.Json.Nodes;

namespace Meshyard.Services;

public sealed class AssetItem
{
    public long Id { get; init; }
    public string Name { get; init; } = "";
    public string Record { get; init; } = "";
    public string Manufacturer { get; init; } = "";
    public string Category { get; init; } = "";
    public string Subcategory { get; init; } = "";
    public string Description { get; init; } = "";

    public string Subtitle => string.Join(" · ", new[] { Manufacturer, Labels.Category(Category), Subcategory, Record }
        .Where(s => !string.IsNullOrWhiteSpace(s)));

    public override string ToString() => Name;  // accessible name of its list row

    public static AssetItem From(JsonNode n) => new()
    {
        Id = n.Long("id"),
        Name = n.Str("name"),
        Record = n.Str("record"),
        Manufacturer = n.Str("manufacturer"),
        Category = n.Str("category"),
        Subcategory = n.Str("subcategory"),
        Description = n.Str("description"),
    };
}

/// <summary>A value with a display label, for combo boxes and suggestion lists.</summary>
public sealed record Choice(string Id, string Label)
{
    public override string ToString() => Label;
}

/// <summary>What the inspector, print and export panels work on: an indexed asset or a built figure.</summary>
public sealed class Target
{
    public AssetItem? Asset { get; init; }
    public string? Figure { get; init; }
    public string? FigureUrl { get; init; }
    public string Name { get; init; } = "";
    public string Meta { get; init; } = "";
    public string Description { get; init; } = "";
    public bool IsFigure => Figure != null;

    /// <summary>The fields that identify it in print / analyze / export requests.</summary>
    public void AddTo(JsonObject body)
    {
        if (Figure != null)
        {
            body["figure"] = Figure;
            body["label"] = Name;
        }
        else body["id"] = Asset!.Id;
    }
}

public sealed record Install(string Channel, string Version, string Change, string Path, bool Indexed,
                             string IndexState, string IndexError)
{
    public string Label => $"{Channel} {Version}" + (Indexed ? "" : " (not indexed)");
    public override string ToString() => Label;
}

public static class Labels
{
    public static string Category(string c) => c switch
    {
        "" => "All",
        "spaceships" => "Ships",
        "groundvehicles" => "Vehicles",
        "scitem" => "Items",
        "geometry" => "Geometry files",
        _ => c.Length > 0 ? char.ToUpper(c[0]) + c[1..] : c,
    };
}

/// <summary>State shared by the app's panels, and the events that keep them in step.</summary>
public sealed class AppState(Api api, Jobs jobs, Viewer viewer)
{
    public Api Api { get; } = api;
    public Jobs Jobs { get; } = jobs;
    public Viewer Viewer { get; } = viewer;

    public List<Install> Installs { get; private set; } = [];
    public string? Channel { get; private set; }
    public Install? CurrentInstall => Installs.FirstOrDefault(i => i.Channel == Channel);
    public JsonObject PrintDefaults { get; private set; } = [];
    public List<Choice> ExportFormats { get; private set; } = [];
    public Dictionary<string, string?> Tools { get; private set; } = [];
    public List<string> Searched { get; private set; } = [];
    public bool HaveBlender => Tools.GetValueOrDefault("blender") != null;
    public bool HaveGameTools => Tools.GetValueOrDefault("starbreaker") != null && Tools.GetValueOrDefault("cgf_converter") != null;

    public Target? Selected { get; private set; }

    public event Action? InstallsLoaded;
    public event Action? ChannelChanged;
    public event Action<Target?>? SelectionChanged;

    public async Task LoadInstallsAsync()
    {
        var d = await Api.GetAsync("api/installs");
        Installs = d.Items("installs").Select(i => new Install(
            i.Str("channel"), i.Str("version"), i["change"]?.ToString() ?? "", i.Str("path"), i.Bool("indexed"),
            i["index"].Str("state", "idle"), i["index"].Str("error"))).ToList();
        PrintDefaults = d["print"]?.DeepClone() as JsonObject ?? [];
        ExportFormats = (d["export_formats"] as JsonArray ?? [])
            .Select(x => new Choice(x![0]!.GetValue<string>(), x[1]!.GetValue<string>())).ToList();
        Tools = (d["tools"] as JsonObject ?? []).ToDictionary(kv => kv.Key, kv => kv.Value?.GetValue<string>());
        Searched = d.Items("searched").Select(s => s.GetValue<string>()).ToList();
        var keep = Channel ?? Prefs.Get("channel");
        var pick = Installs.FirstOrDefault(i => i.Channel == keep)
                   ?? Installs.FirstOrDefault(i => i.Channel == d.Str("default_channel"))
                   ?? Installs.FirstOrDefault();
        var changed = pick?.Channel != Channel;
        Channel = pick?.Channel;
        InstallsLoaded?.Invoke();
        if (changed) ChannelChanged?.Invoke();
    }

    public void SetChannel(string channel)
    {
        if (channel == Channel) return;
        Channel = channel;
        Prefs.Set("channel", channel);
        ChannelChanged?.Invoke();
    }

    public void Select(Target? t)
    {
        Selected = t;
        SelectionChanged?.Invoke(t);
    }
}
