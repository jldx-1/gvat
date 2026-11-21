// Copyright (C) 2021-2022 Intel Corporation
// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import JSZip from 'jszip';

onmessage = async (event) => {
    try {
        const { start, end, block, dimension, dimension2D } = event.data;
        const zip = await JSZip.loadAsync(block);
        const files = Object.values(zip.files).filter((file) => !file.dir);
        if (end >= files.length) throw new Error('Image chunk contains too few frames');
        // Decode one frame at a time and transfer ownership. Cloning bitmaps
        // leaves another full decoded frame in the worker until garbage collection.
        for (let index = start; index <= end; index++) {
            const file = files[index];
            const blob = await file.async('blob');
            if (dimension === dimension2D) {
                const bitmap = await createImageBitmap(blob);
                try {
                    postMessage({ fileName: file.name, index, data: bitmap }, [bitmap]);
                } catch (error) {
                    bitmap.close();
                    throw error;
                }
            } else {
                postMessage({ fileName: file.name, index, data: blob });
            }
        }
    } catch (error) {
        postMessage({ error });
    }
};
