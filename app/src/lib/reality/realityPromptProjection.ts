import {
  realityStore,
} from "./realityRuntime";
import type { RealityStore } from "./realityStore";
import {
  compileRealityContext,
} from "./realityContextCompiler";
import type { CompiledRealityPrompt } from "./realityContextCompiler";

export class RealityPromptProjection {
  private current: CompiledRealityPrompt;
  private readonly listeners = new Set<() => void>();
  private readonly unsubscribeSource: () => void;
  private disposed = false;

  constructor(private readonly store: RealityStore = realityStore) {
    this.current = compileRealityContext(this.store.getSnapshot());
    this.unsubscribeSource = this.store.subscribe(() => {
      this.refresh();
    });
  }

  getSnapshot(): CompiledRealityPrompt {
    return this.current;
  }

  subscribe(listener: () => void): () => void {
    if (this.disposed) {
      return () => {};
    }

    this.listeners.add(listener);
    let subscribed = true;

    return () => {
      if (subscribed) {
        subscribed = false;
        this.listeners.delete(listener);
      }
    };
  }

  dispose(): void {
    if (this.disposed) {
      return;
    }

    this.disposed = true;
    this.unsubscribeSource();
    this.listeners.clear();
  }

  private refresh(): void {
    if (this.disposed) {
      return;
    }

    const next = compileRealityContext(this.store.getSnapshot());
    if (next.text === this.current.text) {
      return;
    }

    this.current = next;
    this.notify();
  }

  private notify(): void {
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch {
        // A projection subscriber failure must not corrupt the compiled result.
      }
    }
  }
}

export const realityPromptProjection = new RealityPromptProjection(
  realityStore,
);
