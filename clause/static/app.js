const $ = (id) => document.getElementById(id);

let sessionId = null;
let selectedMode = "private";
let health = { cloud: false, private: false };

const els = {
  themeToggle: $("themeToggle"),
  dropzone: $("dropzone"),
  fileInput: $("fileInput"),
  docStatus: $("docStatus"),
  docMeta: $("docMeta"),
  modePrivate: $("modePrivate"),
  modeCloud: $("modeCloud"),
  modeHint: $("modeHint"),
  micBtn: $("micBtn"),
  speechStatus: $("speechStatus"),
  chatThread: $("chatThread"),
  chatScroll: $("chatScroll"),
  chatEmpty: $("chatEmpty"),
};

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("clause-theme", theme);
}

function initTheme() {
  const saved = localStorage.getItem("clause-theme");
  if (saved === "dark" || saved === "light") {
    applyTheme(saved);
    return;
  }
  applyTheme(window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

els.themeToggle.addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  applyTheme(next);
});

async function refreshHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    health = data.modes || { cloud: false, private: false };
    updateModeHint();
  } catch {
    health = { cloud: false, private: false };
    els.modeHint.textContent = "Cannot reach the Clause server. Is uvicorn running?";
  }
}

function setMode(mode) {
  selectedMode = mode;
  els.modePrivate.classList.toggle("active", mode === "private");
  els.modeCloud.classList.toggle("active", mode === "cloud");
  updateModeHint();
}

function updateModeHint() {
  if (selectedMode === "cloud") {
    els.modeHint.textContent = health.cloud
      ? "Cloud mode sends your questions (and retrieved lease excerpts) to Gemini."
      : "Add GEMINI_API_KEY on the server to enable Cloud mode, or switch to Private.";
  } else {
    els.modeHint.textContent = health.private
      ? "Private mode runs on your machine — document text stays local."
      : "Set CACTUS_MODEL_PATH (and Cactus bindings) for Private, or use Cloud with an API key.";
  }
}

els.modePrivate.addEventListener("click", () => setMode("private"));
els.modeCloud.addEventListener("click", () => setMode("cloud"));

function setDocMeta(payload) {
  els.docStatus.textContent = payload.filename;
  els.docMeta.hidden = false;
  els.docMeta.innerHTML = `<span style="font-family:var(--mono);font-size:0.82rem">${payload.pages} pp</span> · ${payload.paragraph_count} sections · ${(
    payload.chars / 1000
  ).toFixed(1)}k chars`;
}

function scrollChatDown() {
  requestAnimationFrame(() => {
    els.chatScroll.scrollTop = els.chatScroll.scrollHeight;
  });
}

/** Strip leaked prompt / system-instruction lines small models sometimes echo into the reply. */
function sanitizeAssistantReply(raw) {
  if (!raw) return "";
  let t = raw;
  const junkPatterns = [
    /Acknowledge follow-ups naturally;?\s*stay consistent with earlier answers\.?/gi,
    /Acknowledge follow-ups naturally\.?/gi,
    /stay consistent with earlier answers\.?/gi,
    /Reply conversationally for spoken playback[^.\n]*\.?/gi,
    /About 120[–-]260 words[^.\n]*\.?/gi,
  ];
  for (const re of junkPatterns) {
    t = t.replace(re, "");
  }
  const lines = t.split(/\r?\n/);
  const filtered = lines.filter((line) => {
    const s = line.trim().toLowerCase();
    if (!s) return true;
    if (s.includes("acknowledge follow-ups")) return false;
    if (s.includes("stay consistent with earlier")) return false;
    if (s.includes("reply conversationally for spoken")) return false;
    if (/^what they just said/i.test(line.trim())) return false;
    return true;
  });
  return filtered.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Convert `**bold**` to <strong> after escaping (assistant bubbles only). */
function formatAssistantMarkdownToHtml(plain) {
  const esc = escapeHtml(plain);
  return esc.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

/** Plain text for speech: no HTML, bold markers flattened. */
function assistantTextForSpeech(sanitizedPlain) {
  return sanitizedPlain.replace(/\*\*([^*]+)\*\*/g, "$1").trim();
}

function hideEmptyState() {
  if (els.chatEmpty) els.chatEmpty.hidden = true;
}

function appendUserBubble(text) {
  hideEmptyState();
  const el = document.createElement("div");
  el.className = "msg msg--user";
  el.textContent = text;
  els.chatThread.appendChild(el);
  scrollChatDown();
}

/** Slightly slower, lower pitch — reads closer to natural speech. */
const ASSISTANT_SPEECH_RATE = 0.9;
const ASSISTANT_SPEECH_PITCH = 0.92;

function pickAssistantVoice() {
  const voices = window.speechSynthesis.getVoices();
  if (!voices.length) return null;
  const score = (v) => {
    let s = 0;
    if (!v.lang.startsWith("en")) return -1;
    if (v.lang === "en-US") s += 3;
    if (/Samantha|Aaron|Google US English|Enhanced|Premium|Natural|Siri/i.test(v.name)) s += 5;
    if (v.default) s += 1;
    return s;
  };
  const ranked = [...voices].filter((v) => v.lang.startsWith("en")).sort((a, b) => score(b) - score(a));
  return ranked[0] || voices.find((v) => v.lang.startsWith("en")) || null;
}

/** Speak assistant reply via browser TTS (fallback when ElevenLabs is unavailable). */
function speakAssistantReply(text) {
  if (!text || !window.speechSynthesis) return;

  const run = () => {
    window.speechSynthesis.cancel();
    const utt = new SpeechSynthesisUtterance(text);
    utt.rate = ASSISTANT_SPEECH_RATE;
    utt.pitch = ASSISTANT_SPEECH_PITCH;
    const voice = pickAssistantVoice();
    if (voice) utt.voice = voice;
    window.speechSynthesis.speak(utt);
  };

  if (window.speechSynthesis.getVoices().length) {
    run();
  } else {
    window.speechSynthesis.addEventListener("voiceschanged", run, { once: true });
  }
}

let assistantAudioEl = null;
let assistantAudioObjectUrl = null;

function stopAssistantPlayback() {
  window.speechSynthesis?.cancel();
  if (assistantAudioEl) {
    assistantAudioEl.pause();
    assistantAudioEl.removeAttribute("src");
    assistantAudioEl.load();
    assistantAudioEl = null;
  }
  if (assistantAudioObjectUrl) {
    URL.revokeObjectURL(assistantAudioObjectUrl);
    assistantAudioObjectUrl = null;
  }
}

/** Prefer ElevenLabs MP3 from POST /api/speak; fall back to speechSynthesis. */
async function playAssistantReplyAudio(text) {
  if (!text) return;

  stopAssistantPlayback();

  const fallbackWithLog = (reason) => {
    if (typeof console !== "undefined" && console.warn) {
      console.warn("[Clause TTS] Using browser speech (ElevenLabs unavailable):", reason);
    }
    speakAssistantReply(text);
  };

  try {
    const res = await fetch("/api/speak", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "audio/mpeg,audio/*;q=0.9,*/*;q=0.8",
      },
      body: JSON.stringify({ text }),
    });

    const ct = (res.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();

    if (!res.ok) {
      fallbackWithLog(`HTTP ${res.status}`);
      return;
    }

    if (ct.includes("application/json")) {
      fallbackWithLog("API returned JSON instead of audio");
      return;
    }

    const blob = await res.blob();
    if (!blob?.size) {
      fallbackWithLog("empty audio body");
      return;
    }

    const mimeFromBlob = blob.type || ct || "audio/mpeg";
    const audioBlob =
      mimeFromBlob.includes("mpeg") || mimeFromBlob.includes("mp3") || mimeFromBlob.startsWith("audio/")
        ? blob
        : new Blob([blob], { type: "audio/mpeg" });

    assistantAudioObjectUrl = URL.createObjectURL(audioBlob);
    const audio = new Audio();
    audio.src = assistantAudioObjectUrl;
    audio.preload = "auto";
    assistantAudioEl = audio;
    audio.addEventListener("ended", () => {
      stopAssistantPlayback();
    });
    audio.addEventListener("error", () => {
      fallbackWithLog("audio element error");
    });
    await audio.play().catch((e) => {
      fallbackWithLog(e?.message || "play() rejected");
    });
  } catch (e) {
    fallbackWithLog(e?.message || "fetch failed");
  }
}

function appendAssistantBubble(text, metaLine) {
  hideEmptyState();
  let cleaned = sanitizeAssistantReply(text);
  if (!cleaned.trim() && text && text.trim()) {
    cleaned = text.trim();
  }
  const speechPlain = assistantTextForSpeech(cleaned);

  const el = document.createElement("div");
  el.className = "msg msg--assistant";
  el.setAttribute("role", "article");
  el.setAttribute("tabindex", "0");
  el.title = "Tap to replay this reply";
  const body = document.createElement("div");
  body.className = "msg-text msg-text--rich";
  body.innerHTML = formatAssistantMarkdownToHtml(cleaned);
  const meta = document.createElement("span");
  meta.className = "msg-meta";
  meta.textContent = metaLine;
  el.appendChild(body);
  el.appendChild(meta);
  el.addEventListener("click", () => {
    void playAssistantReplyAudio(speechPlain);
  });
  els.chatThread.appendChild(el);
  scrollChatDown();
  void playAssistantReplyAudio(speechPlain);
}

let typingRow = null;

function showTyping() {
  hideEmptyState();
  typingRow = document.createElement("div");
  typingRow.className = "msg msg--assistant msg--typing";
  for (let i = 0; i < 3; i++) {
    typingRow.appendChild(document.createElement("span"));
  }
  els.chatThread.appendChild(typingRow);
  scrollChatDown();
}

function hideTyping() {
  typingRow?.remove();
  typingRow = null;
}

function resetChat() {
  els.chatThread.innerHTML = "";
  if (els.chatEmpty) els.chatEmpty.hidden = false;
}

async function uploadFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  els.docStatus.textContent = "Uploading…";
  const res = await fetch("/api/upload", { method: "POST", body: fd });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    els.docStatus.textContent = "Upload failed";
    throw new Error(data.detail || res.statusText);
  }
  sessionId = data.session_id;
  resetChat();
  setDocMeta(data);
  return data;
}

els.dropzone.addEventListener("click", () => els.fileInput.click());

els.fileInput.addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  try {
    await uploadFile(file);
  } catch (err) {
    alert(err.message || String(err));
    els.docStatus.textContent = "No file yet";
    els.docMeta.hidden = true;
  }
});

["dragenter", "dragover"].forEach((evt) =>
  els.dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    els.dropzone.classList.add("drag");
  }),
);

["dragleave", "drop"].forEach((evt) =>
  els.dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    els.dropzone.classList.remove("drag");
  }),
);

els.dropzone.addEventListener("drop", async (e) => {
  const file = e.dataTransfer?.files?.[0];
  if (!file) return;
  try {
    await uploadFile(file);
  } catch (err) {
    alert(err.message || String(err));
  }
});

/** @type {SpeechRecognition | null} */
let recognition = null;
if ("webkitSpeechRecognition" in window || "SpeechRecognition" in window) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";
}

/** Transcript for the current mic session (final segments only). */
let pendingTranscript = "";

function setListening(on) {
  els.micBtn.classList.toggle("listening", on);
  els.micBtn.setAttribute("aria-pressed", on ? "true" : "false");
  els.micBtn.setAttribute("aria-label", on ? "Stop and send" : "Start speaking");
  els.speechStatus.textContent = on
    ? "Listening… tap the circle again when you're done"
    : "Tap the mic to speak";
}

function startListeningSession() {
  try {
    recognition.start();
  } catch {
    setTimeout(() => {
      try {
        recognition.start();
      } catch {
        /* already running */
      }
    }, 120);
  }
}

let replyInFlight = false;

async function sendTurn(question) {
  if (!sessionId) {
    alert("Upload a PDF first.");
    return;
  }
  if (question.length < 3) {
    els.speechStatus.textContent = "Didn't catch that — try again.";
    return;
  }
  if (replyInFlight) return;

  replyInFlight = true;
  if (recognition) els.micBtn.disabled = true;

  appendUserBubble(question);
  showTyping();

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, question, mode: selectedMode }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);

    const meta = `${data.source === "gemini" ? "Gemini" : "On-device"} · ${data.excerpts_used} clauses`;
    hideTyping();
    appendAssistantBubble(data.answer, meta);
  } catch (err) {
    hideTyping();
    alert(err.message || String(err));
  } finally {
    replyInFlight = false;
    if (recognition) els.micBtn.disabled = false;
  }
}

if (recognition) {
  let micOn = false;

  recognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const chunk = event.results[i][0].transcript;
      if (!event.results[i].isFinal) continue;
      pendingTranscript = `${pendingTranscript} ${chunk}`.trim();
    }
  };

  recognition.onerror = (event) => {
    const code = event.error || "";
    if (code === "not-allowed" || code === "service-not-allowed") {
      micOn = false;
      pendingTranscript = "";
      setListening(false);
      alert("Microphone permission is required for voice input.");
      return;
    }
  };

  recognition.onend = () => {
    if (micOn) {
      setTimeout(() => {
        if (micOn) startListeningSession();
      }, 75);
    } else {
      setListening(false);
    }
  };

  const toggleMic = () => {
    if (!recognition) return;
    if (!micOn) {
      stopAssistantPlayback();
      pendingTranscript = "";
      micOn = true;
      setListening(true);
      startListeningSession();
    } else {
      micOn = false;
      try {
        recognition.stop();
      } catch {
        /* ignore */
      }
      setListening(false);
      const q = pendingTranscript.trim();
      pendingTranscript = "";
      void sendTurn(q);
    }
  };

  els.micBtn.addEventListener("click", toggleMic);
} else {
  els.micBtn.disabled = true;
  els.speechStatus.textContent = "Voice input isn’t supported in this browser";
}

initTheme();
setMode("private");
refreshHealth().then(() => {
  if (!health.private && health.cloud) {
    setMode("cloud");
  }
});
