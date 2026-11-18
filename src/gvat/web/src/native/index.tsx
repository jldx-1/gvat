// SPDX-License-Identifier: MIT
// Render CVAT's annotation page directly. GVAT supplies only local transport.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { Provider, useSelector } from 'react-redux';
import { MemoryRouter, Route } from 'react-router-dom';
import Modal from 'antd/lib/modal';
import Input from 'antd/lib/input';
import notification from 'antd/lib/notification';
import dayjs from 'dayjs';
import advancedFormat from 'dayjs/plugin/advancedFormat';
import customParseFormat from 'dayjs/plugin/customParseFormat';
import localeData from 'dayjs/plugin/localeData';
import relativeTime from 'dayjs/plugin/relativeTime';
import weekday from 'dayjs/plugin/weekday';
import weekOfYear from 'dayjs/plugin/weekOfYear';
import weekYear from 'dayjs/plugin/weekYear';
import duration from 'dayjs/plugin/duration';
import 'antd/dist/reset.css';
import 'styles.scss';
import AnnotationPage from 'components/annotation-page/annotation-page';
import { ShortcutsContextProvider } from 'components/shortcuts.context';
import ShortcutsDialog from 'components/shortcuts-dialog/shortcuts-dialog';
import ExportDatasetModal, { configureLocalExports } from 'components/export-dataset/export-dataset-modal';
import { changeFrameStep, switchSmoothImage } from 'actions/settings-actions';
import { getCVATStore } from 'cvat-store';
import { getCore, Job, User, FramesMetaData, Issue } from 'cvat-core-wrapper';
import { getCollection } from 'cvat-core/src/annotations';
import coreConfig from 'cvat-core/src/config';
import {
    AnnotationActionTypes, fetchAnnotationsAsync, changeFrameAsync, saveLogsAsync,
} from 'actions/annotation-actions';
import { AuthActionTypes } from 'actions/auth-actions';
import { FormatsActionTypes } from 'actions/formats-actions';
import AnnotationFormats from 'cvat-core/src/annotation-formats';
import { Workspace } from 'reducers';
import { api, setSession } from './api';
import { activateFrame, localFrame } from './frames';
import NativeSettings from './settings';
import { installExports } from './exports';
import './host.scss';

const store = getCVATStore();
for (const plugin of [advancedFormat, customParseFormat, localeData, relativeTime, weekday, weekOfYear, weekYear, duration]) dayjs.extend(plugin);
const cvat = getCore();
let job: Job;
let savedAnnotations = '';
let revision = '';
let boot: any;

function NativeAnnotationPage(): JSX.Element {
    const state = useSelector((value: any) => value.annotation);
    return <AnnotationPage job={state.job.instance} fetching={state.job.fetching}
        annotationsInitialized={state.annotations.initialized} frameNumber={state.player.frame.number}
        workspace={state.workspace} getJob={() => {}} closeJob={() => {}}
        saveLogs={() => store.dispatch(saveLogsAsync() as any)}
        changeFrame={(frame) => store.dispatch(changeFrameAsync(frame) as any)} />;
}

async function start(): Promise<void> {
    boot = await api('session');
    setSession(boot);
    configureLocalExports(boot.settings);
    const defaults = JSON.stringify(boot.settings);
    if (localStorage.getItem('gvat-config-defaults') !== defaults) {
        let cached: any = {};
        try { cached = JSON.parse(localStorage.getItem('clientSettings') || '{}'); } catch (_) { /* restore valid defaults */ }
        cached.player = { ...cached.player, frameStep: boot.settings.frame_step, smoothImage: !boot.settings.pixelated };
        localStorage.setItem('clientSettings', JSON.stringify(cached));
        localStorage.setItem('gvat-config-defaults', defaults);
        store.dispatch(changeFrameStep(boot.settings.frame_step));
        store.dispatch(switchSmoothImage(!boot.settings.pixelated));
    }
    revision = boot.revision;
    coreConfig.localFrameBuffer = { maxBytes: boot.buffer.max_bytes, maxChunks: boot.buffer.max_chunks };
    const user = new User(boot.user);
    store.dispatch({ type: AuthActionTypes.AUTHENTICATED_SUCCESS, payload: { user } });
    boot.job.labels.forEach((label: any, index: number) => {
        label.color = label.color || cvat.enums.colors[index % cvat.enums.colors.length];
    });
    job = new Job(boot.job);
    installExports(job, () => revision);
    store.dispatch({ type: FormatsActionTypes.GET_FORMATS_SUCCESS, payload: { annotationFormats: new AnnotationFormats(await api('formats')) } });
    const meta = new FramesMetaData(boot.meta);
    const nativeGetFrame = job.frames.get.bind(job.frames);
    job.frames.get = async (number, playing = false, step = 1) => {
        activateFrame(number, playing);
        return localFrame(await nativeGetFrame(number, playing, step), number);
    };
    job.frames.save = async () => {};
    job.frames.contextImage = async () => ({});
    await job.annotations.clear({ reload: true });
    savedAnnotations = JSON.stringify(getCollection(job).export());
    job.annotations.hasUnsavedChanges = () => JSON.stringify(getCollection(job).export()) !== savedAnnotations;
    job.annotations.save = async () => {
        const collection = getCollection(job).export();
        const serialized = JSON.stringify(collection);
        const result = await api('annotations', { view: boot.view, revision, annotations: collection, unchanged: serialized === savedAnnotations });
        revision = result.revision;
        savedAnnotations = serialized;
    };
    const firstFrame = await job.frames.get(0);
    const queryParameters = {
        initialOpenGuide: false, initialWorkspace: boot.mode === 'review' ? Workspace.REVIEW : Workspace.STANDARD,
        defaultLabel: null, defaultPointsCount: null, defaultRotated: false,
    };
    store.dispatch({ type: AnnotationActionTypes.GET_JOB_SUCCESS, payload: {
        job, jobMeta: meta, openTime: Date.now(), frameNumbers: boot.frames.map((_: any, i: number) => i),
        frameNumber: 0, frameFilename: firstFrame.filename, frameData: firstFrame, relatedFiles: 0,
        colors: [...cvat.enums.colors], filters: [], queryParameters,
        groundTruthInstance: null, groundTruthJobFramesMeta: null, validationLayout: null,
        issues: boot.issues.map((issue: any) => new Issue(issue)), conflicts: [],
    } });
    await store.dispatch(fetchAnnotationsAsync() as any);
    createRoot(document.getElementById('root')!).render(
        <Provider store={store}><ShortcutsContextProvider><MemoryRouter initialEntries={['/tasks/1/jobs/1']}>
            <Route path='/tasks/:tid/jobs/:jid'><NativeAnnotationPage /></Route><NativeSettings /><ShortcutsDialog /><ExportDatasetModal />
        </MemoryRouter></ShortcutsContextProvider></Provider>,
    );
    await store.dispatch(changeFrameAsync(0, false) as any);
    const shown = new Set<unknown>();
    store.subscribe(() => {
        const errors = store.getState().notifications.errors;
        for (const group of Object.values(errors)) for (const value of Object.values(group || {})) {
            if (value && !shown.has(value)) {
                shown.add(value);
                const failure = value as any;
                notification.error({ message: failure.message || 'Annotation action failed', description: failure.reason?.message || String(failure.reason || '') });
            }
        }
    });
    document.title = 'GVAT';
    setInterval(async () => {
        if (document.hidden) return;
        try {
            const current = await api('revision');
            if (current.revision !== revision) notification.warning({
                key: 'local-xml-changed', message: 'XML changed on disk',
                description: 'Reload this page to use the file. Unsaved edits remain in CVAT.', duration: 0,
            });
        } catch (error) {
            notification.warning({ key: 'local-xml-changed', message: (error as Error).message, duration: 0 });
        }
    }, 4000);
}

window.addEventListener('gvat:copy-feedback', async () => {
    try {
        const { markdown } = await api('feedback');
        try { await navigator.clipboard.writeText(markdown); } catch (_) { /* native dialog also supports manual copying */ }
        Modal.info({ title: 'Review feedback', width: 720, content: <Input.TextArea value={markdown} readOnly rows={14} />, okText: 'Close' });
    } catch (error) { notification.error({ message: (error as Error).message }); }
});
window.addEventListener('beforeunload', (event) => {
    if (job?.annotations.hasUnsavedChanges()) { event.preventDefault(); event.returnValue = ''; }
});
start().catch((error) => {
    document.getElementById('root')!.textContent = `Could not open CVAT: ${error.message}`;
    console.error(error);
});
