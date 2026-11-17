using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.UI;
using Microsoft.UI.Xaml.Controls;

namespace Meshyard.Services;

/// <summary>Drives the three.js viewer page (the engine's /viewer) inside a WebView2.</summary>
public sealed class Viewer
{
    readonly WebView2 _web;
    readonly TaskCompletionSource _ready = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public event Action<string, bool>? Message;

    public Viewer(WebView2 web) => _web = web;

    public async Task InitAsync(Api api, bool dark)
    {
        _web.DefaultBackgroundColor = Colors.Transparent;
        await _web.EnsureCoreWebView2Async();
        var s = _web.CoreWebView2.Settings;
        s.AreDefaultContextMenusEnabled = false;
        s.AreDevToolsEnabled = false;
        s.IsStatusBarEnabled = false;
        s.IsZoomControlEnabled = false;
        s.AreBrowserAcceleratorKeysEnabled = false;
        _web.CoreWebView2.WebMessageReceived += (_, e) =>
        {
            var m = JsonNode.Parse(e.WebMessageAsJson);
            switch (m.Str("type"))
            {
                case "ready": _ready.TrySetResult(); break;
                case "message": Message?.Invoke(m.Str("text"), m.Bool("corner")); break;
            }
        };
        var hint = Uri.EscapeDataString("Pick an asset to preview it");
        _web.Source = new Uri(api.Url($"viewer?theme={(dark ? "dark" : "light")}&hint={hint}"));
        await _ready.Task;
    }

    async Task Call(string fn, params object[] args)
    {
        await _ready.Task;
        var js = $"window.viewer.{fn}({string.Join(",", args.Select(a => JsonSerializer.Serialize(a)))})";
        await _web.ExecuteScriptAsync(js);
    }

    public Task Show(string url) => Call("show", url);
    public Task Clear(string text = "") => Call("clear", text);
    public Task Say(string text, bool corner = false) => Call("message", text, corner);
    public Task ResetView() => Call("reset");
    /// <summary>Draw where the print's stand peg goes (an /api/print/peg answer), or nothing.</summary>
    /// <summary>Show the ship on its stand (the last peg answer's stand), or as usual.</summary>
    public Task StandView(bool on) => Call("standView", on);

    public async Task Peg(JsonNode? peg)
    {
        await _ready.Task;
        await _web.ExecuteScriptAsync($"window.viewer.peg({peg?.ToJsonString() ?? "null"})");
    }
    public Task SetTheme(bool dark) => Call("theme", dark);
}
