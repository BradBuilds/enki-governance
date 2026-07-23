/**
 * Text-to-speech store using the browser's Web Speech API.
 * Zero dependencies, fully offline-capable (voices come from OS), open & free.
 *
 * Later: could be extended with OpenAI TTS / ElevenLabs via a provider switch.
 */

import { browser } from '$app/environment';

export type TtsState = 'idle' | 'speaking' | 'paused' | 'loading';
export type TtsProvider = 'web_speech' | 'google' | 'openai';

function createStore() {
	let state = $state<TtsState>('idle');
	let currentText = $state<string>('');
	let currentOffset = $state<number>(0);  // char index into text
	let voices = $state<SpeechSynthesisVoice[]>([]);
	let voiceName = $state<string>('');
	let rate = $state<number>(1.0);
	let pitch = $state<number>(1.0);
	let volume = $state<number>(1.0);
	let provider = $state<TtsProvider>('web_speech');
	let cloudVoice = $state<string>('en-US-Studio-O');  // Studio = broadcast quality
	let cloudVoices = $state<any[]>([]);
	let availableProviders = $state<Record<string, boolean>>({ web_speech: true, google: false, openai: false });
	let utterance: SpeechSynthesisUtterance | null = null;
	let audioEl: HTMLAudioElement | null = null;
	let audioProgress = $state<number>(0);
	let audioDuration = $state<number>(0);

	// Queue / playlist support — lets the user "play the feed"
	type QueueItem = { id: string; headline: string; body: string; ref?: any };
	let queue = $state<QueueItem[]>([]);
	let queueIndex = $state<number>(-1);

	function loadVoices() {
		if (!browser || !('speechSynthesis' in window)) return;
		const v = window.speechSynthesis.getVoices();
		if (v.length > 0) {
			voices = v;
			// Prefer English default if not set
			if (!voiceName) {
				const preferred = v.find(vo => vo.default && vo.lang.startsWith('en')) ||
					v.find(vo => vo.lang.startsWith('en')) ||
					v[0];
				if (preferred) voiceName = preferred.name;
			}
		}
	}

	if (browser && 'speechSynthesis' in window) {
		loadVoices();
		window.speechSynthesis.onvoiceschanged = loadVoices;

		// Restore saved prefs
		try {
			const saved = localStorage.getItem('enki:tts');
			if (saved) {
				const p = JSON.parse(saved);
				if (p.voice) voiceName = p.voice;
				if (p.rate) rate = p.rate;
				if (p.pitch) pitch = p.pitch;
				if (p.volume != null) volume = p.volume;
				if (p.provider) provider = p.provider;
				if (p.cloudVoice) cloudVoice = p.cloudVoice;
			}
		} catch {}

		// One-time migration: upgrade older auto-picked Neural2/Wavenet/Standard
		// cloudVoice selections to Studio-O (our new broadcast-quality default).
		// Users who explicitly picked a voice will have enki:tts-voice-chosen set
		// when setCloudVoice() runs, so we leave those alone.
		try {
			const userPickedVoice = !!localStorage.getItem('enki:tts-voice-chosen');
			if (!userPickedVoice && cloudVoice && !cloudVoice.includes('Studio')) {
				cloudVoice = 'en-US-Studio-O';
				savePrefs();
			}
		} catch {}

		// Probe backend for available cloud providers
		const hasExplicitPref = !!localStorage.getItem('enki:tts-provider-chosen');
		fetch('/api/tts/status').then(r => r.ok ? r.json() : null).then(data => {
			if (data?.providers) {
				availableProviders = {
					web_speech: data.providers.web_speech?.available ?? true,
					google: data.providers.google?.available ?? false,
					openai: data.providers.openai?.available ?? false,
				};
				// If user has a cloud provider selected but it's not available, fall back
				if (provider !== 'web_speech' && !availableProviders[provider]) {
					provider = 'web_speech';
				}
				// On first run: auto-pick highest-quality available provider
				if (!hasExplicitPref) {
					if (availableProviders.google) {
						provider = 'google';
					} else if (availableProviders.openai) {
						provider = 'openai';
					}
					savePrefs();
				}
				// Load cloud voices if cloud provider selected
				if (provider !== 'web_speech') {
					loadCloudVoices();
				}
			}
		}).catch(() => {});
	}

	function savePrefs() {
		if (!browser) return;
		try {
			localStorage.setItem('enki:tts', JSON.stringify({
				voice: voiceName, rate, pitch, volume, provider, cloudVoice,
			}));
		} catch {}
	}

	async function speakArticle(headline: string, body: string) {
		// Use cloud provider if available + selected
		if (provider !== 'web_speech' && availableProviders[provider]) {
			await speakCloud(headline, body);
		} else {
			const text = headline ? `${headline}. ${body}` : body;
			speak(text);
		}
	}

	async function speakCloud(headline: string, body: string) {
		stop();
		const text = headline && body ? `${headline}\n\n${body}` : (body || headline);
		currentText = text;
		state = 'loading';

		try {
			const res = await fetch('/api/tts/generate', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					headline, body, provider, voice: cloudVoice, rate,
				}),
			});
			if (!res.ok) {
				const err = await res.json().catch(() => ({ detail: 'TTS failed' }));
				console.warn('Cloud TTS failed, falling back to Web Speech:', err.detail);
				speak(headline ? `${headline}. ${body}` : body);
				return;
			}
			const data = await res.json();
			if (!data.audio_url) {
				speak(headline ? `${headline}. ${body}` : body);
				return;
			}

			// Play the audio
			audioEl = new Audio(data.audio_url);
			audioEl.volume = volume;
			audioEl.playbackRate = 1.0;  // server already encoded at requested rate
			audioEl.onplay = () => { state = 'speaking'; };
			audioEl.onpause = () => { if (state === 'speaking') state = 'paused'; };
			audioEl.onended = () => {
				state = 'idle';
				currentOffset = 0;
				audioProgress = 0;
				advanceQueue();
			};
			audioEl.onerror = () => { state = 'idle'; };
			audioEl.ontimeupdate = () => {
				if (audioEl) {
					audioProgress = audioEl.currentTime;
					audioDuration = audioEl.duration || 0;
					// Approximate currentOffset by progress fraction
					if (audioDuration > 0) {
						currentOffset = Math.floor((audioEl.currentTime / audioDuration) * text.length);
					}
				}
			};
			await audioEl.play();
		} catch (e) {
			console.warn('Cloud TTS error, falling back:', e);
			speak(headline ? `${headline}. ${body}` : body);
		}
	}

	function speak(text: string) {
		if (!browser || !('speechSynthesis' in window)) {
			console.warn('Speech synthesis not supported');
			return;
		}
		if (!text) return;
		stop();

		currentText = text;
		currentOffset = 0;

		utterance = new SpeechSynthesisUtterance(text);
		const v = voices.find(vo => vo.name === voiceName);
		if (v) utterance.voice = v;
		utterance.rate = rate;
		utterance.pitch = pitch;
		utterance.volume = volume;

		utterance.onstart = () => { state = 'speaking'; };
		utterance.onend = () => { state = 'idle'; currentOffset = 0; advanceQueue(); };
		utterance.onerror = (e) => {
			// 'canceled'/'interrupted' fire when stop() runs — expected, not an error
			if (e.error !== 'canceled' && e.error !== 'interrupted') {
				console.warn('TTS error:', e.error);
			}
			state = 'idle';
		};
		utterance.onboundary = (e) => {
			if (e.name === 'word' || e.name === 'sentence') {
				currentOffset = e.charIndex;
			}
		};

		window.speechSynthesis.speak(utterance);
		// Chrome has a bug where speech stops after ~15 sec — periodic pause/resume keeps it going
		keepAlive();
	}

	let keepAliveTimer: ReturnType<typeof setInterval> | null = null;
	function keepAlive() {
		if (keepAliveTimer) clearInterval(keepAliveTimer);
		keepAliveTimer = setInterval(() => {
			if (!browser) return;
			if (state === 'speaking' && window.speechSynthesis.speaking && !window.speechSynthesis.paused) {
				window.speechSynthesis.pause();
				window.speechSynthesis.resume();
			} else {
				if (keepAliveTimer) { clearInterval(keepAliveTimer); keepAliveTimer = null; }
			}
		}, 10000);
	}

	function pause() {
		if (!browser) return;
		if (audioEl && !audioEl.paused) {
			audioEl.pause();
			state = 'paused';
			return;
		}
		if (state === 'speaking') {
			window.speechSynthesis.pause();
			state = 'paused';
		}
	}

	function resume() {
		if (!browser) return;
		if (audioEl && audioEl.paused) {
			audioEl.play();
			state = 'speaking';
			return;
		}
		if (state === 'paused') {
			window.speechSynthesis.resume();
			state = 'speaking';
			keepAlive();
		}
	}

	function stop() {
		if (!browser) return;
		if (audioEl) {
			audioEl.pause();
			audioEl.currentTime = 0;
			audioEl = null;
		}
		if ('speechSynthesis' in window) {
			window.speechSynthesis.cancel();
		}
		state = 'idle';
		currentOffset = 0;
		audioProgress = 0;
		audioDuration = 0;
		if (keepAliveTimer) { clearInterval(keepAliveTimer); keepAliveTimer = null; }
	}

	function stopAll() {
		// Full stop — cancel any queue and stop current playback.
		queue = [];
		queueIndex = -1;
		stop();
	}

	function toggle(text: string) {
		if (state === 'speaking' && currentText === text) {
			pause();
		} else if (state === 'paused' && currentText === text) {
			resume();
		} else {
			speak(text);
		}
	}

	function toggleArticle(headline: string, body: string) {
		const key = headline && body ? `${headline}\n\n${body}` : (body || headline);
		if ((state === 'speaking' || state === 'loading') && currentText === key) {
			pause();
		} else if (state === 'paused' && currentText === key) {
			resume();
		} else {
			speakArticle(headline, body);
		}
	}

	// ── Queue playback ──────────────────────────────────────────────
	function playQueueItem(idx: number) {
		if (idx < 0 || idx >= queue.length) {
			queueIndex = -1;
			return;
		}
		queueIndex = idx;
		const item = queue[idx];
		speakArticle(item.headline, item.body);
	}

	function advanceQueue() {
		// Called from audio/utterance onend — only advances when a queue is active
		if (queueIndex < 0 || queue.length === 0) return;
		if (queueIndex + 1 < queue.length) {
			playQueueItem(queueIndex + 1);
		} else {
			queueIndex = -1;
		}
	}

	function playFeed(items: QueueItem[], startAt = 0) {
		if (!items || items.length === 0) return;
		queue = items;
		playQueueItem(Math.max(0, Math.min(startAt, items.length - 1)));
	}

	function nextInQueue() {
		if (queueIndex < 0 || queueIndex + 1 >= queue.length) return;
		playQueueItem(queueIndex + 1);
	}

	function prevInQueue() {
		if (queueIndex <= 0) return;
		playQueueItem(queueIndex - 1);
	}

	function clearQueue() {
		queue = [];
		queueIndex = -1;
	}

	async function loadCloudVoices() {
		if (!browser) return;
		if (provider === 'web_speech') return;
		try {
			const res = await fetch(`/api/tts/voices?provider=${provider}`);
			if (res.ok) {
				const data = await res.json();
				cloudVoices = data.voices || [];
			}
		} catch {}
	}

	return {
		get state() { return state; },
		get currentText() { return currentText; },
		get currentOffset() { return currentOffset; },
		get voices() { return voices; },
		get voiceName() { return voiceName; },
		get rate() { return rate; },
		get pitch() { return pitch; },
		get volume() { return volume; },
		get provider() { return provider; },
		get cloudVoice() { return cloudVoice; },
		get cloudVoices() { return cloudVoices; },
		get availableProviders() { return availableProviders; },
		get audioProgress() { return audioProgress; },
		get audioDuration() { return audioDuration; },
		get queue() { return queue; },
		get queueIndex() { return queueIndex; },
		get queueItem() { return queueIndex >= 0 ? queue[queueIndex] : null; },
		get isPlayingQueue() { return queueIndex >= 0 && queue.length > 0; },
		get isSupported() {
			return browser && 'speechSynthesis' in window;
		},

		setVoice(name: string) { voiceName = name; savePrefs(); },
		setRate(r: number) { rate = Math.max(0.5, Math.min(2.5, r)); savePrefs(); },
		setPitch(p: number) { pitch = Math.max(0.5, Math.min(2.0, p)); savePrefs(); },
		setVolume(v: number) {
			volume = Math.max(0, Math.min(1, v));
			if (audioEl) audioEl.volume = volume;
			savePrefs();
		},
		setProvider(p: TtsProvider) {
			provider = p;
			savePrefs();
			try { localStorage.setItem('enki:tts-provider-chosen', '1'); } catch {}
			loadCloudVoices();
		},
		setCloudVoice(v: string) {
			cloudVoice = v;
			savePrefs();
			try { localStorage.setItem('enki:tts-voice-chosen', '1'); } catch {}
		},
		loadCloudVoices,

		speak, pause, resume, stop, stopAll, toggle,
		speakArticle, toggleArticle,
		playFeed, nextInQueue, prevInQueue, clearQueue,
	};
}

export const tts = createStore();
