using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace SCAssetBrowser.Services;

/// <summary>
/// Looks for a newer release on GitHub each time the app opens, and installs it on request.
///
/// The new exe is downloaded next to the running one and checked against the size and
/// SHA-256 that GitHub reports for the release file. A running exe can't be overwritten,
/// so a small script waits for the app to close, moves the new exe over the old one
/// (keeping whatever name the user gave it) and starts it again.
/// </summary>
public static class Updater
{
    public const string Repo = "10-Gauge/SC-Asset-Browser";
    public const string ReleasesPage = "https://github.com/" + Repo + "/releases/latest";
    const string AssetName = "SC-Asset-Browser.exe";

    public sealed record Release(Version Version, string Url, long Size, string? Sha256, string PageUrl);

    static readonly string? ExePath = Environment.ProcessPath;

    static readonly HttpClient Http = CreateClient();

    static HttpClient CreateClient()
    {
        var h = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
        h.DefaultRequestHeaders.UserAgent.ParseAdd($"SC-Asset-Browser/{Current.ToString(3)}");
        return h;
    }

    public static Version Current
    {
        get
        {
            var v = Assembly.GetExecutingAssembly().GetName().Version ?? new Version(0, 0, 0);
            return new Version(v.Major, v.Minor, Math.Max(v.Build, 0));
        }
    }

    /// <summary>Only the packaged exe updates itself; a development run has no engine inside.</summary>
    public static bool CanSelfUpdate =>
        ExePath != null
        && !Path.GetFileNameWithoutExtension(ExePath).Equals("dotnet", StringComparison.OrdinalIgnoreCase)
        && Assembly.GetExecutingAssembly().GetManifestResourceInfo("sc-engine.exe") != null;

    static string DownloadPath => ExePath + ".download";

    /// <summary>The latest release when it's newer than this build, otherwise null.</summary>
    public static async Task<Release?> CheckAsync()
    {
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(20));
        using var req = new HttpRequestMessage(HttpMethod.Get, $"https://api.github.com/repos/{Repo}/releases/latest");
        req.Headers.Accept.ParseAdd("application/vnd.github+json");
        using var resp = await Http.SendAsync(req, cts.Token);
        if (!resp.IsSuccessStatusCode) return null;
        var json = JsonNode.Parse(await resp.Content.ReadAsStringAsync(cts.Token));
        var tag = (string?)json?["tag_name"] ?? "";
        if (!Version.TryParse(tag.TrimStart('v', 'V'), out var v)) return null;
        v = new Version(v.Major, v.Minor, Math.Max(v.Build, 0));
        if (v <= Current) return null;
        var asset = json?["assets"]?.AsArray()
            .FirstOrDefault(a => string.Equals((string?)a?["name"], AssetName, StringComparison.OrdinalIgnoreCase));
        if (asset == null) return null;
        var digest = (string?)asset["digest"];
        var sha = digest != null && digest.StartsWith("sha256:", StringComparison.OrdinalIgnoreCase) ? digest[7..] : null;
        return new Release(v, (string)asset["browser_download_url"]!, (long)asset["size"]!, sha,
                           (string?)json!["html_url"] ?? ReleasesPage);
    }

    /// <summary>Download the release next to the running exe and check it. Progress goes from 0 to 1.</summary>
    public static async Task DownloadAsync(Release r, IProgress<double>? progress = null)
    {
        var tmp = DownloadPath;
        try
        {
            using var resp = await Http.GetAsync(r.Url, HttpCompletionOption.ResponseHeadersRead);
            resp.EnsureSuccessStatusCode();
            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            long done = 0;
            await using (var src = await resp.Content.ReadAsStreamAsync())
            await using (var dst = File.Create(tmp))
            {
                var buf = new byte[1 << 20];
                int n;
                while ((n = await src.ReadAsync(buf)) > 0)
                {
                    await dst.WriteAsync(buf.AsMemory(0, n));
                    hash.AppendData(buf, 0, n);
                    done += n;
                    progress?.Report(r.Size > 0 ? Math.Min(1.0, (double)done / r.Size) : 0);
                }
            }
            if (done != r.Size)
                throw new IOException($"the download was {done:N0} bytes, expected {r.Size:N0}");
            if (r.Sha256 != null && !Convert.ToHexString(hash.GetHashAndReset()).Equals(r.Sha256, StringComparison.OrdinalIgnoreCase))
                throw new IOException("the download didn't match the release's checksum");
        }
        catch
        {
            try { File.Delete(tmp); } catch { }
            throw;
        }
    }

    /// <summary>Start the script that swaps in the downloaded exe once this process has exited,
    /// then starts the app again. Call it right before exiting.</summary>
    public static void SwapAndRestart()
    {
        var exe = ExePath!;
        var script = Path.Combine(Paths.Home, "update.cmd");
        var text = new StringBuilder()
            .AppendLine("@echo off")
            .AppendLine("chcp 65001 >nul")
            .AppendLine($"set \"PID={Environment.ProcessId}\"")
            .AppendLine($"set \"EXE={exe}\"")
            .AppendLine($"set \"NEW={DownloadPath}\"")
            .AppendLine("set /a tries=0")
            .AppendLine(":wait")
            .AppendLine("tasklist /FI \"PID eq %PID%\" 2>nul | find \" %PID% \" >nul")
            .AppendLine("if errorlevel 1 goto move")
            .AppendLine("ping -n 2 127.0.0.1 >nul")
            .AppendLine("goto wait")
            .AppendLine(":move")
            .AppendLine("move /Y \"%NEW%\" \"%EXE%\" >nul 2>&1")
            .AppendLine("if not errorlevel 1 goto run")
            .AppendLine("set /a tries+=1")
            .AppendLine("if %tries% geq 30 goto run")
            .AppendLine("ping -n 2 127.0.0.1 >nul")
            .AppendLine("goto move")
            .AppendLine(":run")
            .AppendLine("start \"\" \"%EXE%\"")
            .AppendLine("(goto) 2>nul & del \"%~f0\"")
            .ToString();
        File.WriteAllText(script, text, new UTF8Encoding(false));
        Process.Start(new ProcessStartInfo("cmd.exe", $"/c \"{script}\"")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        });
    }

    /// <summary>Remove a download left over from an update that didn't finish.</summary>
    public static void CleanUp()
    {
        if (ExePath == null) return;
        try { if (File.Exists(DownloadPath)) File.Delete(DownloadPath); } catch { }
    }
}
