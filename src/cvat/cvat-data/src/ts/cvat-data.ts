// Copyright (C) 2021-2022 Intel Corporation
// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import { Mutex } from 'async-mutex';
import { MP4Reader, Bytestream } from './3rdparty/mp4';

export class RequestOutdatedError extends Error {}

export enum BlockType {
    MP4VIDEO = 'mp4video',
    ARCHIVE = 'archive',
}

export enum ChunkQuality {
    ORIGINAL = 'original',
    COMPRESSED = 'compressed',
}

export enum DimensionType {
    DIMENSION_1D = '1d',
    DIMENSION_2D = '2d',
    DIMENSION_3D = '3d',
}

export function decodeContextImages(
    block: any, start: number, end: number,
): Promise<Record<string, ImageBitmap>> {
    if (end <= start) return Promise.resolve({});
    return new Promise((resolve, reject) => {
        decodeContextImages.mutex.acquire().then((release) => {
            const owner = decodeContextImages as any;
            let worker: Worker;
            try {
                worker = owner.zipWorker || new Worker(new URL('./unzip_imgs.worker', import.meta.url));
            } catch (error) {
                release();
                reject(error);
                return;
            }
            owner.zipWorker = worker;
            const result: Record<string, ImageBitmap> = {};
            let decoded = 0;
            let settled = false;
            const finish = (error?: Error): void => {
                if (settled) return;
                settled = true;
                worker.onmessage = discardDecodedMessage;
                worker.onerror = null;
                if (error) {
                    for (const bitmap of Object.values(result)) bitmap.close();
                    worker.terminate();
                    if (owner.zipWorker === worker) owner.zipWorker = null;
                }
                release();
                if (error) reject(error);
                else resolve(result);
            };
            worker.onerror = (event) => finish(event.error || new Error(event.message));
            worker.onmessage = (event) => {
                if (settled) {
                    discardDecodedMessage(event);
                    return;
                }
                if (event.data.error) {
                    finish(event.data.error instanceof Error ? event.data.error : new Error(String(event.data.error)));
                    return;
                }
                const { data, fileName } = event.data;
                const key = fileName.split('.')[0];
                if (key in result) {
                    data.close();
                    finish(new Error('Duplicate context image name'));
                    return;
                }
                result[key] = data;
                decoded++;
                if (decoded === end - start) finish();
            };
            try {
                // The context API's end is exclusive; chunk workers use inclusive end.
                worker.postMessage({
                    block, start, end: end - 1,
                    dimension: DimensionType.DIMENSION_2D, dimension2D: DimensionType.DIMENSION_2D,
                });
            } catch (error) {
                finish(error instanceof Error ? error : new Error(String(error)));
            }
        }).catch(reject);
    });
}

decodeContextImages.mutex = new Mutex();

function discardDecodedMessage(event: MessageEvent): void {
    if (event.data?.data instanceof ImageBitmap) event.data.data.close();
}

interface BlockToDecode {
    chunkFrameNumbers: number[];
    chunkIndex: number;
    block: ArrayBuffer | null;
    onDecodeAll(): void;
    onDecode(frame: number, bitmap: ImageBitmap | Blob): void;
    onReject(e: Error): void;
}

export class FrameDecoder {
    private blockType: BlockType;
    /*
        ImageBitmap when decode zip or video chunks
        Blob when 3D dimension
        null when not decoded yet
    */
    private decodedChunks: Record<number, Record<number, ImageBitmap | Blob>>;
    private chunkIsBeingDecoded: BlockToDecode | null;
    private requestedChunkToDecode: BlockToDecode | null;
    private orderedStack: number[];
    private mutex: Mutex;
    private dimension: DimensionType;
    private cachedChunksLimit: number;
    // used for video chunks to get correct side after decoding
    private renderWidth: number;
    private renderHeight: number;
    private zipWorker: Worker | null;
    private videoWorker: Worker | null;
    private getChunkIndex: (frame: number) => number;
    private closed = false;
    private cancelDecode: (() => void) | null = null;
    private decodeScheduled = false;

    constructor(
        blockType: BlockType,
        cachedBlockCount: number,
        getChunkIndex: (frame: number) => number,
        dimension: DimensionType = DimensionType.DIMENSION_2D,
    ) {
        this.mutex = new Mutex();
        this.orderedStack = [];
        this.zipWorker = null;
        this.videoWorker = null;

        this.cachedChunksLimit = Math.max(1, cachedBlockCount);
        this.dimension = dimension;

        this.renderWidth = 1920;
        this.renderHeight = 1080;
        this.getChunkIndex = getChunkIndex;
        this.blockType = blockType;

        this.decodedChunks = {};
        this.requestedChunkToDecode = null;
        this.chunkIsBeingDecoded = null;
    }

    isChunkCached(chunkIndex: number): boolean {
        return chunkIndex in this.decodedChunks;
    }

    hasFreeSpace(): boolean {
        return Object.keys(this.decodedChunks).length < this.cachedChunksLimit;
    }

    cleanup(extra = 1): void {
        // argument allows us to specify how many chunks we want to write after clear
        const chunks = Object.keys(this.decodedChunks).map((chunk: string) => +chunk);
        let { length } = chunks;
        while (length > this.cachedChunksLimit - Math.min(extra, this.cachedChunksLimit)) {
            const lastChunk = this.orderedStack.pop();
            if (typeof lastChunk === 'undefined') {
                return;
            }

            for (const frame of Object.keys(this.decodedChunks[lastChunk])) {
                const data = this.decodedChunks[lastChunk][frame];
                if (data instanceof ImageBitmap) {
                    data.close();
                }
            }

            delete this.decodedChunks[lastChunk];
            length--;
        }
    }

    private validateFrameNumbers(frameNumbers: number[]): void {
        if (!Array.isArray(frameNumbers) || !frameNumbers.length) {
            throw new Error('chunkFrameNumbers must not be empty');
        }

        // ensure is ordered
        for (let i = 1; i < frameNumbers.length; ++i) {
            const prev = frameNumbers[i - 1];
            const current = frameNumbers[i];
            if (current <= prev) {
                throw new Error(
                    'chunkFrameNumbers must be sorted in the ascending order, ' +
                    `got a (${prev}, ${current}) pair instead`,
                );
            }
        }
    }

    requestDecodeBlock(
        block: ArrayBuffer,
        chunkIndex: number,
        chunkFrameNumbers: number[],
        onDecode: (frame: number, bitmap: ImageBitmap | Blob) => void,
        onDecodeAll: () => void,
        onReject: (e: Error) => void,
    ): void {
        this.validateFrameNumbers(chunkFrameNumbers);
        if (this.closed) {
            onReject(new RequestOutdatedError());
            return;
        }
        if (this.chunkIsBeingDecoded?.chunkIndex === chunkIndex) {
            if (this.requestedChunkToDecode) {
                this.requestedChunkToDecode.onReject(new RequestOutdatedError());
                this.requestedChunkToDecode.block = null;
                this.requestedChunkToDecode = null;
            }
            this.chunkIsBeingDecoded.onReject(new RequestOutdatedError());
            this.chunkIsBeingDecoded.onReject = onReject;
            this.chunkIsBeingDecoded.onDecode = onDecode;
            this.chunkIsBeingDecoded.onDecodeAll = onDecodeAll;
            return;
        }

        if (this.requestedChunkToDecode !== null) {
            // Only one waiter owns the mutex queue. Replace its payload instead
            // of retaining a compressed block and waiter for every scrub event.
            this.requestedChunkToDecode.onReject(new RequestOutdatedError());
            this.requestedChunkToDecode.block = null;
            this.requestedChunkToDecode = { chunkFrameNumbers, chunkIndex, block, onDecode, onDecodeAll, onReject };
            return;
        }
        this.requestedChunkToDecode = { chunkFrameNumbers, chunkIndex, block, onDecode, onDecodeAll, onReject };
        if (!this.decodeScheduled) {
            this.decodeScheduled = true;
            void this.startDecode();
        }
    }

    setRenderSize(width: number, height: number): void {
        this.renderWidth = width;
        this.renderHeight = height;
    }

    frame(frameNumber: number): ImageBitmap | Blob | null {
        const chunkIndex = this.getChunkIndex(frameNumber);
        if (chunkIndex in this.decodedChunks) {
            return this.decodedChunks[chunkIndex][frameNumber];
        }

        return null;
    }

    static cropImage(
        imageBuffer: ArrayBuffer,
        imageWidth: number,
        imageHeight: number,
        width: number,
        height: number,
    ): ImageData {
        if (width === imageWidth && height === imageHeight) {
            return new ImageData(new Uint8ClampedArray(imageBuffer), width, height);
        }
        const source = new Uint32Array(imageBuffer);

        const bufferSize = width * height * 4;
        if (imageWidth === width) {
            return new ImageData(new Uint8ClampedArray(imageBuffer, 0, bufferSize), width, height);
        }

        const buffer = new ArrayBuffer(bufferSize);
        const rgbaInt32 = new Uint32Array(buffer);
        const rgbaInt8Clamped = new Uint8ClampedArray(buffer);
        let writeIdx = 0;
        for (let row = 0; row < height; row++) {
            const start = row * imageWidth;
            rgbaInt32.set(source.subarray(start, start + width), writeIdx);
            writeIdx += width;
        }

        return new ImageData(rgbaInt8Clamped, width, height);
    }

    async startDecode(): Promise<void> {
        const releaseMutex = await this.mutex.acquire();
        this.decodeScheduled = false;
        if (this.closed || !this.requestedChunkToDecode) {
            releaseMutex();
            return;
        }
        const request = this.requestedChunkToDecode;
        const { chunkFrameNumbers, chunkIndex } = request;
        this.requestedChunkToDecode = null;
        this.chunkIsBeingDecoded = request;
        const decodedFrames: Record<number, ImageBitmap | Blob> = {};
        let settled = false;
        let decodedCount = 0;

        const finish = (error?: Error): void => {
            if (settled) return;
            settled = true;
            request.block = null;
            this.cancelDecode = null;
            this.chunkIsBeingDecoded = null;
            if (this.zipWorker) {
                // Do not retain the completed chunk and its callbacks in the
                // persistent worker. Close any late transferred bitmap.
                this.zipWorker.onmessage = discardDecodedMessage;
                this.zipWorker.onerror = null;
                if (error) {
                    this.zipWorker.terminate();
                    this.zipWorker = null;
                }
            }
            if (this.videoWorker) {
                this.videoWorker.terminate();
                this.videoWorker = null;
            }
            if (error) {
                for (const data of Object.values(decodedFrames)) {
                    if (data instanceof ImageBitmap) data.close();
                }
            } else {
                this.decodedChunks[chunkIndex] = decodedFrames;
                this.orderedStack.unshift(chunkIndex);
            }
            releaseMutex();
            if (error) request.onReject(error);
            else request.onDecodeAll();
        };
        this.cancelDecode = () => finish(new RequestOutdatedError());
        const accept = (index: number, data: ImageBitmap | Blob): void => {
            if (settled || this.closed) {
                if (data instanceof ImageBitmap) data.close();
                return;
            }
            const frame = chunkFrameNumbers[index];
            if (!Number.isInteger(index) || frame === undefined || frame in decodedFrames) {
                if (data instanceof ImageBitmap) data.close();
                finish(new Error('Unexpected or duplicate frame in decoded chunk'));
                return;
            }
            decodedFrames[frame] = data;
            try {
                request.onDecode(frame, data);
                decodedCount++;
                // ImageBitmap promises may complete out of order.
                if (decodedCount === chunkFrameNumbers.length) finish();
            } catch (error) {
                finish(error instanceof Error ? error : new Error(String(error)));
            }
        };

        try {
            // Replacing an already cached chunk must close its old bitmaps too.
            if (chunkIndex in this.decodedChunks) {
                for (const data of Object.values(this.decodedChunks[chunkIndex])) {
                    if (data instanceof ImageBitmap) data.close();
                }
                delete this.decodedChunks[chunkIndex];
                this.orderedStack = this.orderedStack.filter((index) => index !== chunkIndex);
            }
            this.cleanup(); // Reserve room for the entire in-progress chunk.
            if (this.blockType === BlockType.MP4VIDEO) {
                const worker = new Worker(new URL('./3rdparty/Decoder.worker', import.meta.url));
                this.videoWorker = worker;
                let index = 0;
                worker.onmessage = (event) => {
                    if (settled || event.data.consoleLog) return;
                    const frameIndex = index++;
                    const scaleFactor = Math.ceil(this.renderHeight / event.data.height);
                    const height = Math.round(this.renderHeight / scaleFactor);
                    const width = Math.round(this.renderWidth / scaleFactor);
                    try {
                        createImageBitmap(FrameDecoder.cropImage(
                            event.data.buf, event.data.width, event.data.height, width, height,
                        )).then((bitmap) => accept(frameIndex, bitmap)).catch((error) => finish(error));
                    } catch (error) {
                        finish(error instanceof Error ? error : new Error(String(error)));
                    }
                };
                worker.onerror = (event) => finish(event.error || new Error(event.message));
                worker.postMessage({
                    type: 'Broadway.js - Worker init',
                    options: { rgb: true, reuseMemory: false },
                });
                const reader = new MP4Reader(new Bytestream(request.block));
                reader.read();
                const video = reader.tracks[1];
                const avc = video.trak.mdia.minf.stbl.stsd.avc1.avcC;
                for (const nal of [avc.sps[0], avc.pps[0]]) {
                    worker.postMessage({ buf: nal, offset: 0, length: nal.length });
                }
                for (let sample = 0; sample < video.getSampleCount(); sample++) {
                    for (const nal of video.getSampleNALUnits(sample)) {
                        worker.postMessage({ buf: nal, offset: 0, length: nal.length });
                    }
                }
            } else {
                const worker = this.zipWorker || new Worker(new URL('./unzip_imgs.worker', import.meta.url));
                this.zipWorker = worker;
                worker.onmessage = (event) => {
                    if (event.data.error) {
                        finish(event.data.error instanceof Error ? event.data.error : new Error(String(event.data.error)));
                    } else {
                        accept(event.data.index, event.data.data);
                    }
                };
                worker.onerror = (event) => finish(event.error || new Error(event.message));
                worker.postMessage({
                    block: request.block, start: 0, end: chunkFrameNumbers.length - 1,
                    dimension: this.dimension, dimension2D: DimensionType.DIMENSION_2D,
                });
            }
            // postMessage has copied the encoded input; the decoder no longer owns it.
            request.block = null;
        } catch (error) {
            finish(error instanceof Error ? error : new Error(String(error)));
        }
    }

    public close(): void {
        this.closed = true;
        const pending = this.requestedChunkToDecode;
        this.requestedChunkToDecode = null;
        if (pending) pending.block = null;
        this.cancelDecode?.();
        if (this.zipWorker) {
            this.zipWorker.onmessage = discardDecodedMessage;
            this.zipWorker.onerror = null;
            this.zipWorker.terminate();
            this.zipWorker = null;
        }
        if (this.videoWorker) {
            this.videoWorker.terminate();
            this.videoWorker = null;
        }
        this.cleanup(Number.MAX_SAFE_INTEGER);
        this.orderedStack = [];
        if (pending) pending.onReject(new RequestOutdatedError());
    }

    public cachedChunks(includeInProgress = false): number[] {
        const chunkIsBeingDecoded = (
            includeInProgress && this.chunkIsBeingDecoded ?
                this.chunkIsBeingDecoded.chunkIndex :
                null
        );
        return Object.keys(this.decodedChunks).map((chunkIndex: string) => +chunkIndex).concat(
            ...(chunkIsBeingDecoded !== null ? [chunkIsBeingDecoded] : []),
        ).sort((a, b) => a - b);
    }
}
