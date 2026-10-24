// The call runtime contract (P2H-3B). CallScreen is UI only: it renders this state and
// calls these actions, and never touches capture, playback, routing or the reply stream
// itself. 'web' is the WebView runtime (useWebCallRuntime); 'native' is reserved for the
// Android call engine (P2H-3C). Both speak the same /api/call/* protocol and call ids.
import type { SpeechStatus } from '../nativeSpeech';
import type { CallLine, VoiceCall } from '../voiceCall';

export type { SpeechStatus };

export type CallRuntimeKind = 'web' | 'native';

export type CallRuntime = {
  kind: CallRuntimeKind;
  call: VoiceCall | null;
  lines: CallLine[];
  /** The record line shown after a call ends. */
  record: string;
  error: string;
  busy: boolean;
  /** Clock for the call timer (ms). */
  now: number;
  /** His lines that could not be voiced (text only). */
  voiceless: Record<number, true>;
  /** Hands-free capture (P2F) is available. */
  voiceSupported: boolean;
  /** The fallback recognizer's status, for the hint when the mic is missing. */
  speechStatus: SpeechStatus;
  micSupported: boolean;
  micOn: boolean;
  hearing: boolean;
  /** Her sentence as it is being recognized. */
  partial: string;
  speaking: boolean;
  /** The speaker button exists only where the route can really be switched. */
  speakerAvailable: boolean;
  speaker: boolean;
  answer(): void;
  decline(): void;
  hangup(): void;
  /** She calls him; language is a configured call language code ('' or none = default). */
  dial(language?: string): void;
  testRing(language?: string): void;
  /** Typed text, sent as one of her utterances. */
  say(text: string): void;
  toggleMic(): void;
  interrupt(): void;
  setSpeaker(on: boolean): void;
};
