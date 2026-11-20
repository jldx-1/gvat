// CVAT owns decoding, chunk caching and prefetching. This supplies local bytes
// and cancels obsolete transport requests when the user changes scrub position.
import { api, session, token } from './api';

let desiredChunk = -1;
let desiredFrame = -1;
let generation = 0;
let activation: Promise<unknown> = Promise.resolve();
const pending = new Map<number, { controller: AbortController; promise: Promise<ArrayBuffer> }>();
const lookahead = (): number => session.buffer.prefetch_chunks;

function cancelled(): DOMException { return new DOMException('Frame request replaced by a newer seek', 'AbortError'); }

export function activateFrame(frame: number, playing = false): void {
    desiredFrame = frame;
    const chunk = Math.floor(frame / session.meta.chunk_size);
    if (chunk === desiredChunk) return;
    const continuous = playing && chunk === desiredChunk + 1;
    desiredChunk = chunk;
    if (!continuous) generation++;
    for (const [number, request] of pending) {
        if (!continuous || number < chunk || number > chunk + lookahead()) request.controller.abort();
    }
    activation = api('chunks/activate', { view: session.view, chunk, generation }, 'POST');
    // A cached frame may need no network read. Keep activation failures handled
    // here; any subsequent chunk fetch still receives the original rejection.
    activation.catch(() => {});
}

export async function getChunk(_job: number, chunk: number): Promise<ArrayBuffer> {
    await activation;
    if (chunk < desiredChunk || chunk > desiredChunk + lookahead()) throw cancelled();
    const previous = pending.get(chunk);
    if (previous && !previous.controller.signal.aborted) return previous.promise;
    const controller = new AbortController();
    const epoch = generation;
    const query = new URLSearchParams({ view: session.view, chunk: String(chunk), generation: String(epoch),
        prefetch: chunk === desiredChunk ? '0' : '1' });
    const promise = fetch(`/api/cvat/chunks?${query}`, {
        headers: { Authorization: `Bearer ${token}` }, signal: controller.signal, cache: 'no-store',
    }).then(async (response) => {
        if (!response.ok) {
            const error = await response.json();
            if (error.cancelled) throw cancelled();
            throw new Error(error.error || 'Could not load local video chunk');
        }
        const data = await response.arrayBuffer();
        if (controller.signal.aborted || generation !== epoch) throw cancelled();
        return data;
    }).finally(() => {
        if (pending.get(chunk)?.controller === controller) pending.delete(chunk);
    });
    pending.set(chunk, { controller, promise });
    return promise;
}

export function localFrame(frame: any, number: number): any {
    // Native CVAT treats a rejected frame number as an outdated canvas request,
    // so intentional seek cancellation must not become a user-facing error.
    return new Proxy(frame, { get(target, property, receiver) {
        if (property !== 'data') return Reflect.get(target, property, receiver);
        return async (...args: any[]) => {
            if (desiredFrame !== number) throw number;
            try { return await target.data(...args); } catch (error) {
                if ((error as Error)?.name === 'AbortError' || desiredFrame !== number) throw number;
                throw error;
            }
        };
    } });
}
