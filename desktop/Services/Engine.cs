using System.Diagnostics;
using System.Reflection;
using System.Text;

namespace Meshyard.Services;

/// <summary>
/// Runs the Python engine (game indexing, model export, print conversion) as a hidden
/// child process and finds the localhost address it serves its API on.
///
/// Release builds carry the packaged engine inside the exe and unpack it once per build
/// to %LOCALAPPDATA%\Meshyard\engine. Development builds run the repo's .venv instead,
/// or the exe named by SC_ENGINE.
///
/// The engine exits when its stdin closes, so it never outlives the app, even if the
/// app is killed.
/// </summary>
public sealed class Engine : IDisposable
{
    Process? _proc;
    readonly StringBuilder _log = new();

    public Uri? BaseUri { get; private set; }
    public string LogPath => Path.Combine(Paths.Home, "engine.log");

    public async Task<Uri> StartAsync(CancellationToken ct = default)
    {
        var psi = Locate();
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardInput = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = Encoding.UTF8;
        psi.StandardErrorEncoding = Encoding.UTF8;
        psi.Environment["PYTHONUNBUFFERED"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";

        var ready = new TaskCompletionSource<Uri>(TaskCreationOptions.RunContinuationsAsynchronously);
        _proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
        _proc.OutputDataReceived += (_, e) =>
        {
            if (e.Data == null) return;
            Append(e.Data);
            if (e.Data.StartsWith("READY ", StringComparison.Ordinal) && Uri.TryCreate(e.Data[6..].Trim(), UriKind.Absolute, out var u))
                ready.TrySetResult(u);
        };
        _proc.ErrorDataReceived += (_, e) => { if (e.Data != null) Append(e.Data); };
        _proc.Exited += (_, _) => ready.TrySetException(new InvalidOperationException(
            $"The engine stopped while starting (exit code {SafeExitCode()}).\n{Tail()}"));
        _proc.Start();
        _proc.BeginOutputReadLine();
        _proc.BeginErrorReadLine();

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromMinutes(3));
        await using (timeout.Token.Register(() => ready.TrySetException(new TimeoutException(
            "The engine didn't start within 3 minutes.\n" + Tail()))))
        {
            BaseUri = await ready.Task;
        }
        return BaseUri;
    }

    int SafeExitCode() { try { return _proc?.ExitCode ?? -1; } catch { return -1; } }

    void Append(string line)
    {
        lock (_log)
        {
            _log.AppendLine(line);
            try { File.AppendAllText(LogPath, line + Environment.NewLine); } catch { }
        }
    }

    string Tail()
    {
        lock (_log)
        {
            var lines = _log.ToString().Split('\n', StringSplitOptions.RemoveEmptyEntries);
            return string.Join("\n", lines.TakeLast(8));
        }
    }

    static ProcessStartInfo Locate()
    {
        try { File.WriteAllText(Path.Combine(Paths.Home, "engine.log"), ""); } catch { }

        var custom = Environment.GetEnvironmentVariable("SC_ENGINE");
        if (!string.IsNullOrWhiteSpace(custom) && File.Exists(custom))
            return new ProcessStartInfo(custom);

        var asm = Assembly.GetExecutingAssembly();
        using (var res = asm.GetManifestResourceStream("meshyard-engine.exe"))
        {
            if (res != null)
            {
                // one folder per build of the app, so an update never runs a stale engine
                var dir = Path.Combine(Paths.Home, "engine", asm.ManifestModule.ModuleVersionId.ToString("N")[..12]);
                var exe = Path.Combine(dir, "meshyard-engine.exe");
                if (!File.Exists(exe) || new FileInfo(exe).Length != res.Length)
                {
                    Directory.CreateDirectory(dir);
                    var tmp = exe + ".tmp";
                    using (var f = File.Create(tmp)) res.CopyTo(f);
                    File.Move(tmp, exe, true);
                    CleanOldEngines(dir);
                }
                return new ProcessStartInfo(exe);
            }
        }

        // development: the repo's virtual environment
        for (var d = new DirectoryInfo(AppContext.BaseDirectory); d != null; d = d.Parent)
        {
            var py = Path.Combine(d.FullName, ".venv", "Scripts", "python.exe");
            if (File.Exists(py) && File.Exists(Path.Combine(d.FullName, "pyproject.toml")))
                return new ProcessStartInfo(py, "-m scsandbox.engine") { WorkingDirectory = d.FullName };
        }
        throw new FileNotFoundException("The engine isn't in this build and no development environment was found. " +
                                        "Build with packaging\\build_exe.ps1, or set SC_ENGINE to the engine exe.");
    }

    static void CleanOldEngines(string keep)
    {
        try
        {
            foreach (var d in Directory.GetDirectories(Path.GetDirectoryName(keep)!))
                if (!string.Equals(d, keep, StringComparison.OrdinalIgnoreCase))
                    try { Directory.Delete(d, true); } catch { /* still running elsewhere */ }
        }
        catch { }
    }

    public void Dispose()
    {
        if (_proc == null) return;
        try
        {
            if (!_proc.HasExited)
            {
                _proc.StandardInput.Close();  // the engine exits when its stdin closes
                if (!_proc.WaitForExit(2500)) _proc.Kill(true);
            }
        }
        catch { }
        _proc.Dispose();
        _proc = null;
    }
}
