import type { PendingChatImage } from './chatImageCompression';

export interface PendingComposerFile {
  fileUrl: string;
  fileName: string;
}

type QueuedUpload = {
  files: PendingComposerFile[];
  revision: number;
};

type QueuedImageCompression = {
  files: PendingChatImage[];
  revision: number;
};

export function reservePendingImageCompression(
  ids: Set<string>,
  images: PendingChatImage[],
): void {
  images.forEach((image) => ids.add(image.id));
}

export function releasePendingImageCompression(ids: Set<string>, id: string): boolean {
  return ids.delete(id);
}

export function availableComposerAttachmentSlots(input: {
  maxAttachments: number;
  pendingFiles: number;
  pendingImages: number;
  uploadingFileReservations: number;
}): number {
  return Math.max(
    0,
    input.maxAttachments
      - input.pendingFiles
      - input.pendingImages
      - input.uploadingFileReservations,
  );
}

/** Keep uploads started before a choice from mutating the composer mid-POST. */
export class ComposerUploadCoordinator {
  private choicePosting = false;
  private queuedUpload: QueuedUpload | null = null;
  private queuedImageCompression: QueuedImageCompression | null = null;
  private readonly currentRevision: () => number;
  private readonly commit: (files: PendingComposerFile[]) => void;
  private readonly commitImages: (files: PendingChatImage[]) => void;

  constructor(
    currentRevision: () => number,
    commit: (files: PendingComposerFile[]) => void,
    commitImages: (files: PendingChatImage[]) => void = () => {},
  ) {
    this.currentRevision = currentRevision;
    this.commit = commit;
    this.commitImages = commitImages;
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
    const queuedImages = this.queuedImageCompression;
    this.queuedImageCompression = null;
    if (queuedImages && queuedImages.revision === this.currentRevision()) {
      this.commitImages(queuedImages.files);
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

  async settleImages(upload: Promise<PendingChatImage[]>, revision: number): Promise<boolean> {
    const files = await upload;
    if (!files.length) return false;
    if (revision !== this.currentRevision()) return true;
    if (this.choicePosting) {
      this.queuedImageCompression = { files, revision };
      return true;
    }
    this.commitImages(files);
    return true;
  }
}
