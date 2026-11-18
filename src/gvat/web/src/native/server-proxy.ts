// Preserve CVAT's core/model implementations. Replace only their remote transport.
import original from '../../../../cvat/cvat-core/src/server-proxy.ts';
import { api, session } from './api';
import { getChunk } from './frames';

export { sleep, PREVIEW_DEFAULT } from '../../../../cvat/cvat-core/src/server-proxy.ts';

export default {
    ...original,
    frames: { ...original.frames, getMeta: async () => ({ ...session.meta, deleted_frames: [] }), getData: getChunk },
    annotations: { ...original.annotations, getAnnotations: async () => session.annotations },
    events: { ...original.events, save: async () => undefined },
    issues: {
        ...original.issues,
        get: async () => session.issues,
        create: async (issue: any) => api('issues', issue, 'POST'),
        update: async (id: number, changes: any) => api(`issues/${id}`, changes),
        delete: async (id: number) => api(`issues/${id}`, undefined, 'DELETE'),
    },
    comments: {
        ...original.comments,
        create: async (comment: any) => api('comments', comment, 'POST'),
    },
};
