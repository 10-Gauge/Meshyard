using System.Collections.ObjectModel;
using System.Text.Json.Nodes;
using Microsoft.UI.Xaml.Controls;

namespace SCAssetBrowser.Services;

public sealed class JobFailedException(string message) : Exception(message);

/// <summary>One running or finished background job, as shown in the activity panel.</summary>
public sealed class JobItem : Observable
{
    string _label = "", _detail = "", _state = "running";

    public string Label { get => _label; set { if (Set(ref _label, value)) Raise(nameof(Title)); } }
    public string Detail { get => _detail; set => Set(ref _detail, value); }

    public string State
    {
        get => _state;
        set
        {
            if (!Set(ref _state, value)) return;
            Raise(nameof(IsRunning));
            Raise(nameof(Severity));
            Raise(nameof(Title));
        }
    }

    public bool IsRunning => State == "running";
    public InfoBarSeverity Severity => State switch
    {
        "done" => InfoBarSeverity.Success,
        "error" => InfoBarSeverity.Error,
        _ => InfoBarSeverity.Informational,
    };
    /// <summary>An error notice rather than a job: its label is already the title.</summary>
    public bool IsNotice { get; init; }
    public string Title => IsNotice ? Label : State switch { "done" => $"{Label}: done", "error" => $"{Label}: failed", _ => Label };
}

/// <summary>Polls the engine's background jobs and lists them for the activity panel.</summary>
public sealed class Jobs(Api api)
{
    public ObservableCollection<JobItem> Items { get; } = [];

    /// <summary>Wait for a job; reports each new log line; returns its result or throws.</summary>
    public async Task<JsonNode> RunAsync(string jobId, Action<string>? onLog = null)
    {
        var item = new JobItem { Label = "Working…" };
        Items.Insert(0, item);
        var seen = 0;
        while (true)
        {
            JsonNode j;
            try { j = await api.GetAsync($"api/jobs/{jobId}"); }
            catch (Exception e)
            {
                item.State = "error";
                item.Detail = e.Message;
                throw new JobFailedException(e.Message);
            }
            item.Label = j.Str("label", item.Label);
            var log = (j["log"] as JsonArray)?.Select(x => x?.GetValue<string>() ?? "").ToList() ?? [];
            for (; seen < log.Count; seen++) onLog?.Invoke(log[seen]);
            item.Detail = string.Join("\n", log.TakeLast(3).Select(l => l.Trim()).Where(l => l.Length > 0));
            var state = j.Str("state", "running");
            if (state == "done")
            {
                item.State = "done";
                item.Detail = "";
                _ = RemoveLater(item);
                return j["result"] ?? new JsonObject();
            }
            if (state == "error")
            {
                var err = j.Str("error", "failed");
                item.State = "error";
                item.Detail = err;
                throw new JobFailedException(err);
            }
            await Task.Delay(700);
        }
    }

    async Task RemoveLater(JobItem item)
    {
        await Task.Delay(5000);
        Items.Remove(item);
    }
}
