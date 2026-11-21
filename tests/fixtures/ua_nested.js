onmessage = () => { const w = new Worker('/ua_worker.js'); w.onmessage = e => postMessage(e.data); w.postMessage('go'); };
