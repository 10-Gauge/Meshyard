using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace Meshyard.Services;

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
    public const string Repo = "10-Gauge/Meshyard";
    public const string ReleasesPage = "https://github.com/" + Repo + "/releases/latest";
    // The repo's earlier name. GitHub redirects it once the repo is renamed, and it's
    // tried when the new name isn't found yet.
    const string OldRepo = "10-Gauge/SC-Asset-Browser";
    // Releases carry the exe under its current name and under the name builds before
    // 1.7.0 look for, so older installs can still update.
    static readonly string[] AssetNames = { "Meshyard.exe", "SC-Asset-Browser.exe" };

    public sealed record Release(Version Version, string Url, long Size, string? Sha256, string PageUrl);

    static readonly string? ExePath = Environment.ProcessPath;

    static readonly HttpClient Http = CreateClient();

    static HttpClient CreateClient()
    {
        var h = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
        h.DefaultRequestHeaders.UserAgent.ParseAdd($"Meshyard/{Current.ToString(3)}");
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
        && Assembly.GetExecutingAssembly().GetManifestResourceInfo("meshyard-engine.exe") != null;

    static string DownloadPath => ExePath + ".download";

    /// <summary>The latest release when it's newer than this build, otherwise null.</summary>
    public static async Task<Release?> CheckAsync()
    {
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(20));
        var json = await LatestReleaseAsync(Repo, cts.Token) ?? await LatestReleaseAsync(OldRepo, cts.Token);
        if (json == null) return null;
        var tag = (string?)json["tag_name"] ?? "";
        if (!Version.TryParse(tag.TrimStart('v', 'V'), out var v)) return null;
        v = new Version(v.Major, v.Minor, Math.Max(v.Build, 0));
        if (v <= Current) return null;
        var assets = json["assets"]?.AsArray();
        var asset = AssetNames
            .Select(n => assets?.FirstOrDefault(a => string.Equals((string?)a?["name"], n, StringComparison.OrdinalIgnoreCase)))
            .FirstOrDefault(a => a != null);
        if (asset == null) return null;
        var digest = (string?)asset["digest"];
        var sha = digest != null && digest.StartsWith("sha256:", StringComparison.OrdinalIgnoreCase) ? digest[7..] : null;
        return new Release(v, (string)asset["browser_download_url"]!, (long)asset["size"]!, sha,
                           (string?)json!["html_url"] ?? ReleasesPage);
    }

    static async Task<JsonNode?> LatestReleaseAsync(string repo, CancellationToken ct)
    {
        using var req = new HttpRequestMessage(HttpMethod.Get, $"https://api.github.com/repos/{repo}/releases/latest");
        req.Headers.Accept.ParseAdd("application/vnd.github+json");
        using var resp = await Http.SendAsync(req, ct);
        if (!resp.IsSuccessStatusCode) return null;
        return JsonNode.Parse(await resp.Content.ReadAsStringAsync(ct));
    }

    /// <summary>Download the release next to the running exe and check it. Progress goes from 0 to 1.
    /// The copy runs off the UI thread. If no data arrives for a while the connection is dropped and
    /// the download picks up where it stopped, so a stalled transfer can't sit at one percent forever.</summary>
    public static async Task DownloadAsync(Release r, IProgress<double>? progress = null)
    {
        var tmp = DownloadPath;
        try
        {
            await Task.Run(() => CopyWithResumeAsync(r, tmp, progress)).ConfigureAwait(false);
            using var hash = SHA256.Create();
            long size;
            await using (var f = File.OpenRead(tmp))
            {
                size = f.Length;
                if (size != r.Size)
                    throw new IOException($"the download was {size:N0} bytes, expected {r.Size:N0}");
                var digest = await hash.ComputeHashAsync(f).ConfigureAwait(false);
                if (r.Sha256 != null && !Convert.ToHexString(digest).Equals(r.Sha256, StringComparison.OrdinalIgnoreCase))
                    throw new IOException("the download didn't match the release's checksum");
            }
        }
        catch
        {
            try { File.Delete(tmp); } catch { }
            throw;
        }
    }

    static readonly TimeSpan StallLimit = TimeSpan.FromSeconds(30);
    const int MaxTries = 6;

    static async Task CopyWithResumeAsync(Release r, string tmp, IProgress<double>? progress)
    {
        File.Delete(tmp);
        long done = 0;
        int lastPct = -1;
        Exception? last = null;
        for (int attempt = 0; attempt < MaxTries && done < r.Size; attempt++)
        {
            if (attempt > 0) await Task.Delay(TimeSpan.FromSeconds(2 * attempt)).ConfigureAwait(false);
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, r.Url);
                if (done > 0) req.Headers.Range = new System.Net.Http.Headers.RangeHeaderValue(done, null);
                using var headCts = new CancellationTokenSource(StallLimit);
                using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, headCts.Token).ConfigureAwait(false);
                resp.EnsureSuccessStatusCode();
                if (done > 0 && resp.StatusCode != System.Net.HttpStatusCode.PartialContent)
                    done = 0;   // the server ignored the range, start over
                await using var src = await resp.Content.ReadAsStreamAsync().ConfigureAwait(false);
                await using var dst = new FileStream(tmp, done > 0 ? FileMode.Append : FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20, true);
                var buf = new byte[1 << 20];
                while (true)
                {
                    using var readCts = new CancellationTokenSource(StallLimit);
                    int n = await src.ReadAsync(buf, readCts.Token).ConfigureAwait(false);
                    if (n == 0) break;
                    await dst.WriteAsync(buf.AsMemory(0, n)).ConfigureAwait(false);
                    done += n;
                    int pct = r.Size > 0 ? (int)Math.Min(100, done * 100 / r.Size) : 0;
                    if (pct != lastPct) { lastPct = pct; progress?.Report(pct / 100.0); }
                }
            }
            catch (Exception ex) when (ex is HttpRequestException or IOException or OperationCanceledException)
            {
                last = ex;   // stalled or dropped: try again from where it stopped
            }
        }
        if (done < r.Size)
            throw new IOException(last is OperationCanceledException
                ? "the download stalled"
                : $"the download stopped early ({last?.Message ?? "connection closed"})");
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
