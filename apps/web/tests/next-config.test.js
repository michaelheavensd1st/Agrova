import { createRequire } from 'node:module';
import { describe, expect, it } from 'vitest';

const require = createRequire(import.meta.url);
const configPath = require.resolve('../next.config.js');
const turboConfig = require('../../../turbo.json');

async function withProxyTarget(target, assertion) {
  const originalTarget = process.env.API_PROXY_TARGET;
  if (target === undefined) delete process.env.API_PROXY_TARGET;
  else process.env.API_PROXY_TARGET = target;
  delete require.cache[configPath];

  try {
    await assertion(() => require(configPath));
  } finally {
    if (originalTarget === undefined) delete process.env.API_PROXY_TARGET;
    else process.env.API_PROXY_TARGET = originalTarget;
    delete require.cache[configPath];
  }
}

describe('Next.js API proxy configuration', () => {
  it('makes the server-side proxy target available to the web build', () => {
    expect(turboConfig.tasks.build.env).toContain('API_PROXY_TARGET');
  });

  it('rewrites the same-origin API path to the host-development API by default', async () => {
    await withProxyTarget(undefined, async (loadConfig) => {
      const config = loadConfig();
      await expect(config.rewrites()).resolves.toEqual([
        {
          source: '/api-proxy/:path*',
          destination: 'http://127.0.0.1:8000/api/:path*',
        },
      ]);
    });
  });

  it.each(['http://api:8000/', 'http://api:8000///', 'https://api.example.test/'])(
    'accepts and normalizes HTTP(S) origin %s',
    async (target) => {
      await withProxyTarget(target, async (loadConfig) => {
        const config = loadConfig();
        const origin = target.replace(/\/+$/, '');
        await expect(config.rewrites()).resolves.toEqual([
          {
            source: '/api-proxy/:path*',
            destination: `${origin}/api/:path*`,
          },
        ]);
      });
    },
  );

  it.each([
    '',
    '   ',
    ' http://api:8000',
    '/api',
    'ftp://api:8000',
    'http://api:8000/base',
    'http://user:secret@api:8000',
    'http://api:8000?debug=1',
  ])('rejects invalid proxy target %j', async (target) => {
    await withProxyTarget(target, async (loadConfig) => {
      expect(loadConfig).toThrow(/API_PROXY_TARGET/);
    });
  });
});
