using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace SCAssetBrowser.Services;

/// <summary>The toolkit's data folder (shared with the engine) and small helpers around it.</summary>
public static class Paths
{
    /// <summary>%LOCALAPPDATA%\sc-sandbox, or SCSANDBOX_HOME when set (same rule as the engine).</summary>
    public static string Home { get; } = ResolveHome();

    public static string Exports => Path.Combine(Home, "exports");

    static string ResolveHome()
    {
        var env = Environment.GetEnvironmentVariable("SCSANDBOX_HOME");
        var home = !string.IsNullOrWhiteSpace(env)
            ? env
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "sc-sandbox");
        Directory.CreateDirectory(home);
        return home;
    }

    public static string WriteLog(string name, string text)
    {
        var p = Path.Combine(Home, name);
        try { File.WriteAllText(p, text); } catch { }
        return p;
    }

    /// <summary>Open a folder in Explorer, or select a file in it.</summary>
    public static void Reveal(string path)
    {
        try
        {
            if (File.Exists(path))
                Process.Start(new ProcessStartInfo("explorer.exe", $"/select,\"{path}\"") { UseShellExecute = true });
            else if (Directory.Exists(path))
                Process.Start(new ProcessStartInfo("explorer.exe", $"\"{path}\"") { UseShellExecute = true });
        }
        catch { }
    }

    public static void OpenFile(string path)
    {
        try { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); } catch { }
    }

    public static void OpenUrl(string url)
    {
        try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); } catch { }
    }
}

/// <summary>Front-end preferences (theme, last install, last export format), in desktop.json.</summary>
public static class Prefs
{
    static readonly string FilePath = Path.Combine(Paths.Home, "desktop.json");
    static JsonObject? _data;

    static JsonObject Data
    {
        get
        {
            if (_data != null) return _data;
            try { _data = JsonNode.Parse(File.ReadAllText(FilePath)) as JsonObject; } catch { }
            return _data ??= new JsonObject();
        }
    }

    public static string Get(string key, string fallback = "") =>
        Data[key] is JsonValue v && v.TryGetValue(out string? s) && s != null ? s : fallback;

    public static void Set(string key, string value)
    {
        Data[key] = value;
        try { File.WriteAllText(FilePath, Data.ToJsonString(new JsonSerializerOptions { WriteIndented = true })); } catch { }
    }
}
