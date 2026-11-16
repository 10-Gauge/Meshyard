using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace SCAssetBrowser.Services;

public sealed class ApiException(string message, bool needsIndex) : Exception(message)
{
    public bool NeedsIndex { get; } = needsIndex;
}

/// <summary>JSON client for the engine's localhost API.</summary>
public sealed class Api(Uri baseUri)
{
    readonly HttpClient _http = new() { BaseAddress = baseUri, Timeout = TimeSpan.FromMinutes(5) };

    public Uri BaseUri { get; } = baseUri;

    public string Url(string path) => new Uri(BaseUri, path.TrimStart('/')).ToString();

    public static string Query(params (string Key, object? Value)[] q)
    {
        var parts = q.Where(p => p.Value is not null && p.Value.ToString() != "")
                     .Select(p => $"{Uri.EscapeDataString(p.Key)}={Uri.EscapeDataString(Convert.ToString(p.Value, System.Globalization.CultureInfo.InvariantCulture)!)}");
        var s = string.Join("&", parts);
        return s.Length > 0 ? "?" + s : "";
    }

    public Task<JsonNode> GetAsync(string path, params (string Key, object? Value)[] q) =>
        SendAsync(new HttpRequestMessage(HttpMethod.Get, path.TrimStart('/') + Query(q)));

    public Task<JsonNode> PostAsync(string path, JsonObject body) =>
        SendAsync(new HttpRequestMessage(HttpMethod.Post, path.TrimStart('/'))
        {
            Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json"),
        });

    /// <summary>Start a background job and return its id.</summary>
    public async Task<string> StartJobAsync(string path, JsonObject body) =>
        (await PostAsync(path, body))["job"]!.GetValue<string>();

    async Task<JsonNode> SendAsync(HttpRequestMessage req)
    {
        using var res = await _http.SendAsync(req);
        var text = await res.Content.ReadAsStringAsync();
        JsonNode? node = null;
        try { node = JsonNode.Parse(text); } catch (JsonException) { }
        if (!res.IsSuccessStatusCode)
        {
            var msg = node?["error"]?.GetValue<string>() ?? res.ReasonPhrase ?? "request failed";
            var needs = node?["needs_index"]?.GetValue<bool>() ?? false;
            throw new ApiException(msg, needs);
        }
        return node ?? new JsonObject();
    }
}

public static class Json
{
    public static string Str(this JsonNode? n, string key, string fallback = "") =>
        n?[key] is JsonValue v && v.TryGetValue(out string? s) && s != null ? s : fallback;

    public static long Long(this JsonNode? n, string key, long fallback = 0)
    {
        if (n?[key] is not JsonValue v) return fallback;
        if (v.TryGetValue(out long l)) return l;
        if (v.TryGetValue(out double d)) return (long)d;
        return fallback;
    }

    public static double Num(this JsonNode? n, string key, double fallback = 0)
    {
        if (n?[key] is not JsonValue v) return fallback;
        if (v.TryGetValue(out double d)) return d;
        if (v.TryGetValue(out long l)) return l;
        return fallback;
    }

    public static bool Bool(this JsonNode? n, string key, bool fallback = false) =>
        n?[key] is JsonValue v && v.TryGetValue(out bool b) ? b : fallback;

    public static IEnumerable<JsonNode> Items(this JsonNode? n, string key) =>
        (n?[key] as JsonArray)?.Where(x => x != null).Select(x => x!) ?? [];
}
