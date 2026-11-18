const path = require('path');
const webpack = require('webpack');
const HtmlWebpackPlugin = require('html-webpack-plugin');
const CopyPlugin = require('copy-webpack-plugin');
const upstream = path.resolve(__dirname, '../../cvat');
const ui = path.join(upstream, 'cvat-ui/src');

module.exports = {
    context: __dirname,
    entry: './src/native/index.tsx',
    output: { path: path.join(__dirname, 'dist'), filename: 'editor.[contenthash].js', clean: true },
    resolve: {
        extensions: ['.tsx', '.ts', '.js', '.jsx', '.json'],
        modules: [ui, 'node_modules', path.join(__dirname, 'node_modules')],
        alias: {
            ...Object.fromEntries(['cvat-core', 'cvat-data', 'cvat-canvas', 'cvat-canvas3d'].map((name) => [name, path.join(upstream, name)])),
            'react$': require.resolve('react'),
            'react-dom$': require.resolve('react-dom'),
            'react-dom/client$': require.resolve('react-dom/client'),
            'react-redux$': require.resolve('react-redux'),
            'dayjs$': require.resolve('dayjs'),
            'react-router$': require.resolve('react-router'),
            'react-router-dom$': require.resolve('react-router-dom'),
        },
        fallback: { fs: false },
    },
    resolveLoader: { modules: [path.join(__dirname, 'node_modules')] },
    module: { rules: [
        { test: /\.[jt]sx?$/, exclude: /node_modules/, use: { loader: 'babel-loader', options: { presets: [['@babel/preset-env', { targets: { chrome: '110' } }], ['@babel/preset-react', { runtime: 'classic' }], '@babel/preset-typescript'] } } },
        { test: /\.(css|scss)$/, use: ['style-loader', 'css-loader', 'sass-loader'] },
        { test: /\.svg$/, exclude: /node_modules/, use: ['babel-loader', { loader: 'react-svg-loader', options: { jsx: false, svgo: { plugins: [{ cleanupIDs: false }] } } }] },
        { test: /\.(png|jpg|jpeg|gif|webp|wasm|woff2?|ttf)$/i, type: 'asset/resource' },
    ] },
    plugins: [
        new webpack.DefinePlugin({ 'process.env.GVAT_LOCAL': JSON.stringify('1') }),
        new webpack.NormalModuleReplacementPlugin(/^\.\/server-proxy$/, (resource) => {
            if (resource.context.includes('cvat-core')) resource.request = path.join(__dirname, 'src/native/server-proxy.ts');
        }),
        new HtmlWebpackPlugin({ template: './src/native/index.html', title: 'GVAT' }),
        new CopyPlugin({ patterns: [
            { from: path.join(upstream, 'cvat-ui/src/assets/opencv_4.8.0.js'), to: 'assets/opencv_4.8.0.js' },
            { from: path.join(upstream, 'cvat-ui/src/assets/*.{png,webp}'), to: 'assets/[name][ext]' },
            { from: path.join(upstream, 'cvat-data/src/ts/3rdparty/avc.wasm'), to: 'assets/3rdparty/avc.wasm' },
        ] }),
    ],
    optimization: { minimize: false, splitChunks: { chunks: 'all' } },
    performance: { hints: false },
};
