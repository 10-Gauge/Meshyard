using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Meshyard.Services;

/// <summary>The toolkit's data folder (shared with the engine) and small helpers around it.</summary>
public static class Paths
{
    /// <summary>%LOCALAPPDATA%\Meshyard, or MESHYARD_HOME when set (same rule as the engine).</summary>
    public static string Home { get; } = ResolveHome();

    public static string Exports => Path.Combine(Home, "exports");

    static string ResolveHome()
    {
        var env = Environment.GetEnvironmentVariable("MESHYARD_HOME");
        if (string.IsNullOrWhiteSpace(env)) env = Environment.GetEnvironmentVariable("SCSANDBOX_HOME");
        var home = !string.IsNullOrWhiteSpace(env) ? env : Migrate();
        Directory.CreateDirectory(home);
        // the engine (a child process) reads the same variable, so both always agree
        Environment.SetEnvironmentVariable("MESHYARD_HOME", home);
        return home;
    }

    /// <summary>The data folder used to be %LOCALAPPDATA%\sc-sandbox. Move it to
    /// %LOCALAPPDATA%\Meshyard the first time, and fix the paths saved in its settings.
    /// If something in it is still open (an update finishing, say), keep using the old
    /// folder this time and try again on the next start.</summary>
    static string Migrate()
    {
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var home = Path.Combine(local, "Meshyard");
        var old = Path.Combine(local, "sc-sandbox");
        if (Directory.Exists(home) || !Directory.Exists(old)) return home;
        try { Directory.Move(old, home); }
        catch { return old; }
        foreach (var name in new[] { "settings.json", "desktop.json" })
        {
            var f = Path.Combine(home, name);
            try
            {
                var text = File.ReadAllText(f);
                var fixedText = text.Replace(old, home).Replace(old.Replace("\\", "\\\\"), home.Replace("\\", "\\\\"));
                if (fixedText != text) File.WriteAllText(f, fixedText);
            }
            catch { }
        }
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
