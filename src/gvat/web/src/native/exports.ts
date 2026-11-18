// Feed CVAT's native, interpolated annotation states to its Datumaro exporters.
import notification from 'antd/lib/notification';
import { api, session, token } from './api';
import { getCollection } from 'cvat-core/src/annotations';

function serialize(state: any): any {
    const attributes = Object.fromEntries(state.label.attributes.map((spec: any) => [spec.name,
        state.attributes[spec.id] ?? spec.defaultValue]));
    return { type: state.objectType === 'tag' ? 'tag' : state.shapeType, label_id: state.label.id,
        points: state.points || [], attributes, group: state.group?.id || 0,
        occluded: state.occluded, rotation: state.rotation || 0, z_order: state.zOrder || 0,
        source: state.source, id: state.clientID, keyframe: state.keyframe,
        ...(state.objectType === 'track' ? { track_id: state.serverID ?? state.clientID } : {}),
        elements: state.elements?.map(serialize) || [] };
}

export function installExports(job: any, revision: () => string): void {
    job.annotations.exportDataset = async (format: string, saveImages: boolean, _defaults: boolean, storage: any, name?: string) => {
        if (job.annotations.hasUnsavedChanges()) throw new Error('Save annotations before exporting.');
        if (storage.location !== 'local') throw new Error('Local exports download through your browser.');
        const key = 'local-annotation-export';
        notification.info({ key, message: 'Exporting annotations', description: 'Preparing the download…', duration: 0 });
        let identifier: string | undefined;
        const snapshot = JSON.stringify(getCollection(job).export());
        try {
            const started = await api('exports', { view: session.view, revision: revision(), format, save_images: saveImages, name }, 'POST');
            identifier = started.id;
            if (started.collect_frames) {
                let frames: any[] = [];
                for (let frame = 0; frame < session.frames.length; frame++) {
                    const states = await job.annotations.get(frame, false, []);
                    frames.push({ frame, annotations: states.filter((state: any) => !state.outside).map(serialize) });
                    if (frames.length === 100 || frame === session.frames.length - 1) {
                        await api(`exports/${identifier}`, { frames });
                        frames = [];
                    }
                }
            }
            if (JSON.stringify(getCollection(job).export()) !== snapshot) throw new Error('Annotations changed during export. Save and export again.');
            const response = await fetch(`/api/cvat/exports/${identifier}`, { headers: { Authorization: `Bearer ${token}` } });
            if (!response.ok) throw new Error((await response.json()).error || 'Could not export annotations');
            const url = URL.createObjectURL(await response.blob());
            const link = document.createElement('a');
            link.href = url;
            link.download = started.filename;
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 2000);
            notification.success({ key, message: 'Annotation download ready' });
        } catch (error) {
            notification.error({ key, message: 'Annotation export failed', description: (error as Error).message });
            throw error;
        } finally {
            if (identifier) await api(`exports/${identifier}`, undefined, 'DELETE').catch(() => {});
        }
    };
}
