onmessage = async () => postMessage({ua: navigator.userAgent, ch: navigator.userAgentData?.brands, headers: await fetch('/headers').then(r => r.json())});
