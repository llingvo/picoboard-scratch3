// PicoBoard Extension for TurboWarp / Scratch
(function() {
    const GLOBAL_KEY = '_picoboardExtensionInstance';

    // 如果已有实例，先卸载
    if (window[GLOBAL_KEY]) {
        console.log('[PicoBoard] Previous instance found, unloading...');
        window[GLOBAL_KEY].unload();
    }

    class PicoBoardExtension {
        constructor(runtime) {
            this.runtime = runtime;
            this.ws = null;
            this.values = {
                slider: 0,
                light: 0,
                sound: 0,
                button: 0,
                A: 0,
                B: 0,
                C: 0,
                D: 0
            };
            this.connect();
        }

        connect() {
            const WS_URL = 'ws://127.0.0.1:8765';
            this.ws = new WebSocket(WS_URL);

            this.ws.onopen = () => {
                console.log('[PicoBoard] Connected to WebSocket');
            };

            this.ws.onmessage = (msg) => {
                try {
                    const data = JSON.parse(msg.data);
                    Object.assign(this.values, data);
                } catch (e) {
                    console.error('[PicoBoard] Invalid message', msg.data);
                }
            };

            this.ws.onclose = () => {
                console.log('[PicoBoard] WebSocket closed, retry in 1s');
                setTimeout(() => this.connect(), 1000);
            };

            this.ws.onerror = (e) => {
                console.error('[PicoBoard] WebSocket error', e);
                if (this.ws) this.ws.close();
            };
        }

        getInfo() {
            return {
                id: 'picoboard',
                name: 'PicoBoard',
                blocks: [
                    { opcode: 'slider', blockType: 'reporter', text: 'Slider' },
                    { opcode: 'light', blockType: 'reporter', text: 'Light' },
                    { opcode: 'sound', blockType: 'reporter', text: 'Sound' },
                    { opcode: 'button', blockType: 'Boolean', text: 'Button Pressed?' },
                    { opcode: 'A', blockType: 'reporter', text: 'Aux A' },
                    { opcode: 'B', blockType: 'reporter', text: 'Aux B' },
                    { opcode: 'C', blockType: 'reporter', text: 'Aux C' },
                    { opcode: 'D', blockType: 'reporter', text: 'Aux D' }
                ]
            };
        }

        // Block implementations
        slider() { return this.values.slider; }
        light() { return this.values.light; }
        sound() { return this.values.sound; }
        button() { return this.values.button === 1; }
        A() { return this.values.A; }
        B() { return this.values.B; }
        C() { return this.values.C; }
        D() { return this.values.D; }

        // 卸载扩展
        unload() {
            if (this.ws) {
                this.ws.close();
                this.ws = null;
            }
            console.log('[PicoBoard] Extension unloaded');
        }
    }

    // 注册并保存实例到全局
    const extensionInstance = new PicoBoardExtension();
    Scratch.extensions.register(extensionInstance);
    window[GLOBAL_KEY] = extensionInstance;

})();