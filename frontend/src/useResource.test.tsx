import { cleanup, renderHook, waitFor, act } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { useResource } from './useResource';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('запоздалый ответ прежней страницы не заменяет актуальные данные', async () => {
  let old!: (value: Response) => void;
  vi.stubGlobal('fetch', vi.fn((url: string) => url === '/old'
    ? new Promise<Response>(resolve => { old = resolve; }) : Promise.resolve(Response.json('new'))));
  const hook = renderHook(({ url }) => useResource<string>(url), { initialProps: { url: '/old' } });
  hook.rerender({ url: '/new' });
  await waitFor(() => expect(hook.result.current.data).toBe('new'));
  await act(async () => { old(Response.json('old')); });
  expect(hook.result.current.data).toBe('new');
});

test('ответ начатого чтения не отменяет результат сохранения', async () => {
  let finish!: (value: Response) => void;
  let calls = 0;
  vi.stubGlobal('fetch', vi.fn(() => ++calls === 1 ? new Promise<Response>(resolve => { finish = resolve; }) : Promise.resolve(Response.json('saved'))));
  const hook = renderHook(() => useResource<string>('/item'));
  act(() => hook.result.current.setData('saved'));
  await act(async () => { finish(Response.json('old')); });
  expect(hook.result.current.data).toBe('saved');
});
