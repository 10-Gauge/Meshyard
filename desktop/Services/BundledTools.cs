using System.IO.Compression;
using System.Text;
using System.Text.Json;

namespace SCAssetBrowser.Services;

/// <summary>
/// The tools the engine runs (StarBreaker, its Blender add-on, cgf-converter and, in a
/// -WithBlender build, a portable Blender) travel inside the exe: packaging\build_exe.ps1 appends a zip of
/// them after the app, followed by its length and the marker "SCTOOLS1". On start each
/// tool is unpacked once to %LOCALAPPDATA%\sc-sandbox\tools\&lt;tool&gt; (where the engine
/// looks first), and again only when a new app build carries a different version.
///
/// A tool the user has since updated with "Download tools" (its folder has no BUNDLED
/// marker) is left alone. Each tool is unpacked into a staging folder and swapped in
/// whole, so the engine never sees half a tool.
/// </summary>
public static class BundledTools
{
    static readonly byte[] Magic = Encoding.ASCII.GetBytes("SCTOOLS1");

    public static string ToolsDir => Path.Combine(Paths.Home, "tools");

    /// <summary>Names of the bundled tools that need unpacking, in bundle order (empty when none).</summary>
    public static IReadOnlyList<string> Pending()
    {
        using var zip = Open(out var manifest);
        if (zip == null || manifest == null) return [];
        return manifest.Where(kv => NeedsUnpack(kv.Key, kv.Value)).Select(kv => kv.Key).ToList();
    }

    /// <summary>Unpack the given tools (all pending ones when null); progress gets a line per tool.</summary>
    public static void Unpack(IEnumerable<string>? which = null, IProgress<string>? progress = null)
    {
        using var zip = Open(out var manifest);
        if (zip == null || manifest == null) return;
        var want = (which ?? manifest.Keys).ToHashSet(StringComparer.OrdinalIgnoreCase);
        foreach (var (tool, version) in manifest)
        {
            if (!want.Contains(tool) || !NeedsUnpack(tool, version)) continue;
            progress?.Report(tool);
            var dest = Path.Combine(ToolsDir, tool);
            var staging = Path.Combine(ToolsDir, $".{tool}.bundle");
            if (Directory.Exists(staging)) Directory.Delete(staging, true);
            Directory.CreateDirectory(staging);
            var root = Path.GetFullPath(staging) + Path.DirectorySeparatorChar;
            var prefix = tool + "/";
            foreach (var e in zip.Entries)
            {
                if (!e.FullName.StartsWith(prefix, StringComparison.Ordinal)) continue;
                var target = Path.GetFullPath(Path.Combine(staging, e.FullName[prefix.Length..]));
                if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase)) continue;  // unsafe path
                if (e.FullName.EndsWith('/')) { Directory.CreateDirectory(target); continue; }
                Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                e.ExtractToFile(target, true);
            }
            File.WriteAllText(Path.Combine(staging, "VERSION"), version);
            File.WriteAllText(Path.Combine(staging, "BUNDLED"), version);
            if (Directory.Exists(dest)) Directory.Delete(dest, true);
            Directory.Move(staging, dest);
        }
    }

    static bool NeedsUnpack(string tool, string version)
    {
        var dir = Path.Combine(ToolsDir, tool);
        if (!Directory.Exists(dir)) return true;
        var bundled = Path.Combine(dir, "BUNDLED");
        if (!File.Exists(bundled)) return false;            // the user's own download: keep it
        return File.ReadAllText(bundled).Trim() != version;
    }

    /// <summary>The zip appended to this exe and its manifest (tool -> version), or null.</summary>
    static ZipArchive? Open(out Dictionary<string, string>? manifest)
    {
        manifest = null;
        try
        {
            var exe = Environment.ProcessPath;
            if (exe == null) return null;
            var fs = new FileStream(exe, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            if (fs.Length < 16) { fs.Dispose(); return null; }
            var footer = new byte[16];
            fs.Seek(-16, SeekOrigin.End);
            fs.ReadExactly(footer);
            if (!footer.AsSpan(8).SequenceEqual(Magic)) { fs.Dispose(); return null; }
            var len = BitConverter.ToInt64(footer, 0);
            var start = fs.Length - 16 - len;
            if (len <= 0 || start < 0) { fs.Dispose(); return null; }
            var zip = new ZipArchive(new SubStream(fs, start, len), ZipArchiveMode.Read);
            var m = zip.GetEntry("bundle.json");
            if (m == null) { zip.Dispose(); return null; }
            using (var s = m.Open())
                manifest = JsonSerializer.Deserialize<Dictionary<string, string>>(s);
            return zip;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>A read-only window onto part of a stream (the zip at the end of the exe).</summary>
    sealed class SubStream(Stream inner, long start, long length) : Stream
    {
        long _pos;
        public override bool CanRead => true;
        public override bool CanSeek => true;
        public override bool CanWrite => false;
        public override long Length => length;
        public override long Position { get => _pos; set => _pos = Math.Clamp(value, 0, length); }
        public override int Read(byte[] buffer, int offset, int count)
        {
            var n = (int)Math.Min(count, length - _pos);
            if (n <= 0) return 0;
            inner.Seek(start + _pos, SeekOrigin.Begin);
            n = inner.Read(buffer, offset, n);
            _pos += n;
            return n;
        }
        public override long Seek(long offset, SeekOrigin origin) => Position = origin switch
        {
            SeekOrigin.Begin => offset,
            SeekOrigin.Current => _pos + offset,
            _ => length + offset,
        };
        public override void Flush() { }
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        protected override void Dispose(bool disposing) { if (disposing) inner.Dispose(); base.Dispose(disposing); }
    }
}
