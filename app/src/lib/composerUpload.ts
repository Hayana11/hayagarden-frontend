export interface PendingComposerFile {
  fileUrl: string;
  fileName: string;
}

type QueuedUpload = {
  files: PendingComposerFile[];
  revision: number;
};

/** Keep uploads started before a choice from mutating the composer mid-POST. */
export class ComposerUploadCoordinator {
  private choicePosting = false;
  private queuedUpload: QueuedUpload | null = null;
  private readonly currentRevision: () => number;
  private readonly commit: (files: PendingComposerFile[]) => void;

  constructor(
    currentRevision: () => number,
    commit: (files: PendingComposerFile[]) => void,
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
      this.commit(queued.files);
    }
  }

  async settle(
    upload: Promise<PendingComposerFile[]>,
    revision: number,
  ): Promise<boolean> {
    const files = await upload;
    if (!files.length) return false;
    if (revision !== this.currentRevision()) return true;
    if (this.choicePosting) {
      this.queuedUpload = { files, revision };
      return true;
    }
    this.commit(files);
    return true;
  }
}
