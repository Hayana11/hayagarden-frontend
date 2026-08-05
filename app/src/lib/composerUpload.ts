export interface PendingComposerFile {
  fileUrl: string;
  fileName: string;
}

type QueuedUpload = {
  file: PendingComposerFile;
  revision: number;
};

/** Keep an upload started before a choice without mutating the composer mid-POST. */
export class ComposerUploadCoordinator {
  private choicePosting = false;
  private queuedUpload: QueuedUpload | null = null;
  private readonly currentRevision: () => number;
  private readonly commit: (file: PendingComposerFile) => void;

  constructor(
    currentRevision: () => number,
    commit: (file: PendingComposerFile) => void,
  ) {
    this.currentRevision = currentRevision;
    this.commit = commit;
  }

  beginChoicePost(): void {
    this.choicePosting = true;
  }

  endChoicePost(): void {
    this.choicePosting = false;
    const queued = this.queuedUpload;
    this.queuedUpload = null;
    if (queued && queued.revision === this.currentRevision()) {
      this.commit(queued.file);
    }
  }

  async settle(
    upload: Promise<PendingComposerFile | null>,
    revision: number,
  ): Promise<boolean> {
    const file = await upload;
    if (!file) return false;
    if (revision !== this.currentRevision()) return true;
    if (this.choicePosting) {
      this.queuedUpload = { file, revision };
      return true;
    }
    this.commit(file);
    return true;
  }
}
