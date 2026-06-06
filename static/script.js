(() => {
    "use strict";

    // ------------------------------------------------------------
    // Element refs
    // ------------------------------------------------------------
    const html = document.documentElement;
    const themeBtn = document.getElementById("theme-toggle");
    const form = document.getElementById("download-form");
    const urlInput = document.getElementById("url");
    const submitBtn = document.getElementById("submit-btn");

    const progressBox = document.getElementById("progress");
    const progressStage = document.getElementById("progress-stage");
    const progressDetail = document.getElementById("progress-detail");
    const progressPct = document.getElementById("progress-pct");
    const barFill = document.getElementById("progress-bar");
    const trackList = document.getElementById("track-list");
    const resultBox = document.getElementById("result");
    const resultLink = document.getElementById("result-link");
    const resultText = document.getElementById("result-text");
    const toast = document.getElementById("toast");

    // ------------------------------------------------------------
    // Theme
    // ------------------------------------------------------------
    const storedTheme = localStorage.getItem("theme");
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    if (storedTheme === "dark" || (!storedTheme && prefersDark)) {
        html.classList.add("dark-mode");
    }
    themeBtn.addEventListener("click", () => {
        html.classList.toggle("dark-mode");
        localStorage.setItem("theme",
            html.classList.contains("dark-mode") ? "dark" : "light");
    });

    // ------------------------------------------------------------
    // Toast
    // ------------------------------------------------------------
    let toastTimer = null;
    function showToast(message, kind = "info") {
        toast.textContent = message;
        toast.className = "toast show " + kind;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => {
            toast.classList.remove("show");
        }, 4000);
    }

    // ------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------
    function setLoading(loading) {
        submitBtn.disabled = loading;
        submitBtn.classList.toggle("is-loading", loading);
    }

    function resetProgress() {
        progressStage.textContent = "Starting up...";
        progressDetail.innerHTML = "&nbsp;";
        progressPct.textContent = "0%";
        barFill.style.width = "0%";
        barFill.classList.add("indeterminate");
        trackList.innerHTML = "";
        resultBox.classList.add("hidden");
        resultLink.removeAttribute("href");
    }

    function showProgress() {
        progressBox.classList.remove("hidden");
        progressBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    function fmtBytes(n) {
        if (!n) return "";
        const u = ["B", "KB", "MB", "GB"];
        let i = 0;
        while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
        return n.toFixed(n < 10 && i > 0 ? 1 : 0) + " " + u[i];
    }
    function fmtSpeed(bps) {
        return bps ? fmtBytes(bps) + "/s" : "";
    }
    function fmtEta(s) {
        if (s == null) return "";
        if (s < 60) return s + "s";
        const m = Math.floor(s / 60);
        const r = s % 60;
        return `${m}m ${r.toString().padStart(2, "0")}s`;
    }

    // ------------------------------------------------------------
    // Track-list management
    // ------------------------------------------------------------
    function ensureTrackEl(index, title) {
        let el = trackList.querySelector(`[data-i="${index}"]`);
        if (!el) {
            el = document.createElement("li");
            el.className = "track active";
            el.dataset.i = String(index);
            el.innerHTML = `
                <span class="dot"></span>
                <span class="tname"></span>
                <span class="tmeta"></span>
            `;
            trackList.appendChild(el);
        }
        el.classList.remove("done");
        el.classList.add("active");
        el.querySelector(".tname").textContent = title || `Track ${index}`;
        el.querySelector(".tmeta").textContent = "";
        return el;
    }
    function setTrackMeta(index, text) {
        const el = trackList.querySelector(`[data-i="${index}"] .tmeta`);
        if (el) el.textContent = text;
    }
    function markTrackDone(index, text) {
        const el = trackList.querySelector(`[data-i="${index}"]`);
        if (!el) return;
        el.classList.remove("active");
        el.classList.add("done");
        if (text) el.querySelector(".tmeta").textContent = text;
        else el.querySelector(".tmeta").textContent = "tagged";
    }

    // ------------------------------------------------------------
    // State machine
    // ------------------------------------------------------------
    let activeJobId = null;
    let activeSource = null;

    function handleMessage(msg) {
        switch (msg.type) {
            case "stage": {
                if (msg.stage === "starting") {
                    progressStage.textContent = "Connecting to YouTube...";
                    barFill.classList.add("indeterminate");
                } else if (msg.stage === "tagging") {
                    progressStage.textContent =
                        `Enriching metadata for ${msg.total || 0} track${msg.total === 1 ? "" : "s"}`;
                    barFill.classList.remove("indeterminate");
                    barFill.style.width = "100%";
                    progressPct.textContent = "100%";
                }
                break;
            }
            case "downloading": {
                barFill.classList.remove("indeterminate");
                if (msg.percent != null) {
                    const scaled = Math.min(90, msg.percent * 0.9);
                    barFill.style.width = scaled.toFixed(1) + "%";
                    progressPct.textContent = scaled.toFixed(0) + "%";
                } else {
                    barFill.classList.add("indeterminate");
                }
                const det = [fmtSpeed(msg.speed), fmtEta(msg.eta)]
                    .filter(Boolean).join(" · ");
                progressStage.textContent = msg.filename
                    ? `Downloading ${msg.filename}` : "Downloading audio...";
                progressDetail.textContent = det || (msg.filename || "");
                break;
            }
            case "postprocess": {
                progressStage.textContent = "Converting to MP3...";
                barFill.classList.remove("indeterminate");
                barFill.style.width = "92%";
                progressPct.textContent = "92%";
                progressDetail.textContent = msg.filename || "";
                break;
            }
            case "track": {
                progressStage.textContent =
                    `Tagging track ${msg.index} of ${msg.total}`;
                progressDetail.textContent = msg.title || "";
                ensureTrackEl(msg.index, msg.title);
                break;
            }
            case "metadata": {
                if (msg.stage === "looking_up") {
                    progressDetail.textContent =
                        `Looking up "${msg.title}"${msg.artist ? " by " + msg.artist : ""} on iTunes...`;
                } else if (msg.stage === "done") {
                    const bits = [];
                    if (msg.artist) bits.push(msg.artist);
                    if (msg.album) bits.push(msg.album);
                    if (msg.year) bits.push(msg.year);
                    if (msg.genre) bits.push(msg.genre);
                    if (msg.cover) bits.push("cover art");
                    // Find the most recent active track
                    const lastActive = [...trackList.querySelectorAll(".track.active")]
                        .pop();
                    if (lastActive) {
                        const idx = Number(lastActive.dataset.i);
                        setTrackMeta(idx, bits.join(" · ") || "tagged");
                    }
                }
                break;
            }
            case "done": {
                barFill.classList.remove("indeterminate");
                barFill.style.width = "100%";
                progressPct.textContent = "100%";
                if (msg.kind === "zip") {
                    progressStage.textContent =
                        `Playlist ready (${msg.count} tracks)`;
                    resultText.textContent = `Download playlist (${msg.count} tracks).zip`;
                } else {
                    progressStage.textContent = "Your track is ready";
                    resultText.textContent = `Download "${msg.filename}"`;
                }
                resultLink.href = msg.url;
                resultLink.setAttribute("download", msg.filename || "track");
                resultBox.classList.remove("hidden");
                // Auto-trigger the browser save dialog
                const a = document.createElement("a");
                a.href = msg.url;
                a.download = msg.filename || "track";
                a.rel = "noopener";
                document.body.appendChild(a);
                a.click();
                a.remove();
                showToast(
                    msg.kind === "zip"
                        ? "Playlist zip ready — check your downloads"
                        : "Track ready — check your downloads",
                    "success"
                );
                break;
            }
            case "error": {
                progressStage.textContent = "Something went wrong";
                progressDetail.textContent = msg.message || "";
                barFill.classList.remove("indeterminate");
                barFill.style.width = "0%";
                progressPct.textContent = "—";
                showToast(msg.message || "Download failed", "error");
                break;
            }
        }
    }

    function closeJob() {
        if (activeSource) {
            try { activeSource.close(); } catch (e) { /* ignore */ }
            activeSource = null;
        }
        activeJobId = null;
        setLoading(false);
    }

    // ------------------------------------------------------------
    // Form submission
    // ------------------------------------------------------------
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const url = (urlInput.value || "").trim();
        if (!url || !/^https?:\/\//i.test(url)) {
            showToast("Please paste a valid http(s) URL.", "error");
            urlInput.focus();
            return;
        }

        if (activeJobId) closeJob();

        setLoading(true);
        resetProgress();
        showProgress();

        try {
            const fd = new FormData(form);
            const r = await fetch("/download/start", { method: "POST", body: fd });
            if (!r.ok) {
                const txt = await r.text();
                let msg = "Failed to start download.";
                try { msg = JSON.parse(txt).error || msg; } catch (_) { /* */ }
                throw new Error(msg);
            }
            const data = await r.json();
            activeJobId = data.job_id;

            activeSource = new EventSource("/download/stream?job_id=" + encodeURIComponent(activeJobId));
            activeSource.onmessage = (ev) => {
                try {
                    const msg = JSON.parse(ev.data);
                    handleMessage(msg);
                    if (msg.type === "done" || msg.type === "error") {
                        if (msg.type === "done") {
                            // Mark any remaining active tracks as done
                            trackList.querySelectorAll(".track.active").forEach((el) => {
                                el.classList.remove("active");
                                el.classList.add("done");
                            });
                        }
                        closeJob();
                    }
                } catch (err) {
                    console.error("Bad SSE message", err);
                }
            };
            activeSource.onerror = () => {
                if (activeJobId) {
                    // SSE auto-reconnects; only treat as fatal if the server
                    // actually closed the stream (which happens after 'done').
                    if (activeSource && activeSource.readyState === EventSource.CLOSED) {
                        closeJob();
                    }
                }
            };
        } catch (err) {
            console.error(err);
            showToast(err.message || "Network error", "error");
            closeJob();
        }
    });

    // ------------------------------------------------------------
    // Visual nicety: auto-grow input on focus + Cmd/Ctrl+V support
    // ------------------------------------------------------------
    urlInput.addEventListener("paste", () => {
        // Just to be sure the trimmed value lands in the form on submit
        setTimeout(() => { urlInput.value = urlInput.value.trim(); }, 0);
    });
})();
