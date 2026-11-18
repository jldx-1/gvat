// Copyright (C) 2019-2022 Intel Corporation
// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

const config = {
    backendAPI: '/api',
    organization: {
        organizationID: null,
        organizationSlug: null,
    },
    origin: '',
    uploadChunkSize: 100,
    opencvPath: '',
    removeUnderlyingMaskPixels: {
        enabled: false,
        onEmptyMaskOccurrence: null,
    },
    onOrganizationChange: null,
    globalObjectsCounter: 0,

    requestsStatusDelay: null,

    jobMetaDataReloadPeriod: 1 * 60 * 60 * 1000, // 1 hour

    previewPlaceholders: {} as Record<string, string>,
    // Local filesystem host only; the normal CVAT deployment keeps its defaults.
    localFrameBuffer: null as { maxBytes: number; maxChunks: number } | null,
};

export default config;
