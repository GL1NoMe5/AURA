// Internal formation follows the incident; it is not a user-selectable mode.
export type SceneMode = 'streams' | 'portrait';
export type SceneSettings = { mode: SceneMode; paused: boolean; reducedMotion: boolean; quality: 'full' | 'balanced'; resetId: number; particleCount: number; activeNode?: number };
export type SceneStats = { fps: number; cpuMs: number; gpu: string; points: number; calls: number; geometries: number; textures: number; width: number; height: number; portraitReady: boolean; frames: number; seconds: number; heapBytes: number | null };
