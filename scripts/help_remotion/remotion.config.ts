import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('png');
Config.setOverwriteOutput(true);
// The screenshots are the document; never let the bundler recompress them.
Config.setChromiumOpenGlRenderer('angle');
