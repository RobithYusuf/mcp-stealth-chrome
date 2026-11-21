self.onmessage = async e => e.ports[0].postMessage({ua: navigator.userAgent, ch: navigator.userAgentData?.brands, headers: await fetch('/headers').then(r => r.json())});
