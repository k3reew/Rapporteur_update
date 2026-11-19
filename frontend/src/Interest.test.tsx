import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Interest } from './Interest';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('неопределённый отбор требует конкретного отдела и сохраняет версию', async () => {
  const api = vi.fn(async (_url: string, _init?: RequestInit) => { void _url; void _init; return Response.json({}); });
  vi.stubGlobal('fetch', api);
  const refresh = vi.fn();
  render(<Interest talkId="talk" refresh={refresh} value={{ version: 3, state: 'uncertain', eligible: false, stale: false, manual: false, error: null, department_ids: [], items: [{ department_id: 'dep', department_name: 'Отдел исследований', status: 'uncertain', reason: 'Нужна проверка', evidence: [], paragraphs: [] }] }} />);
  fireEvent.click(screen.getByText('Интерес требует решения'));
  expect((screen.getByRole('button', { name: 'Подтвердить интерес' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText('Отдел исследований'));
  fireEvent.click(screen.getByRole('button', { name: 'Подтвердить интерес' }));
  await vi.waitFor(() => expect(refresh).toHaveBeenCalled());
  expect(JSON.parse(api.mock.calls[0][1]?.body as string)).toEqual({ version: 3, department_ids: ['dep'] });
});

test('показывает автоматический выбор после завершения того же расчёта', async () => {
  const value = { version: 1, state: 'queued', eligible: false, stale: false, manual: false, error: null, department_ids: [], items: [] };
  const { rerender } = render(<Interest talkId="t" refresh={() => {}} value={value} />);
  rerender(<Interest talkId="t" refresh={() => {}} value={{ ...value, state: 'relevant', eligible: true, department_ids: ['d'], items: [{ department_id: 'd', department_name: 'Отдел', status: 'relevant', reason: 'Связь', evidence: [], paragraphs: [] }] }} />);
  fireEvent.click(screen.getByText('Есть интерес отделов: Отдел'));
  expect((screen.getByLabelText('Отдел') as HTMLInputElement).checked).toBe(true);
});

test('называет отдел и уверенность модели в процентах, цитаты свёрнуты', () => {
  const quote = { label: 'S2', quote: 'Средняя задержка чтения достигала 18 мс' };
  render(<Interest talkId="t" refresh={() => {}} value={{ version: 1, state: 'relevant', eligible: true, stale: false, manual: false, error: null, department_ids: ['it'], items: [
    { department_id: 'it', department_name: 'Отдел ИТ', status: 'relevant', interest: 85, reason: 'Задержка запросов к СУБД', evidence: [quote], paragraphs: [] },
    { department_id: 'agro', department_name: 'Отдел агрономии', status: 'not_relevant', interest: 5, reason: 'Связи нет', evidence: [], paragraphs: [] },
  ] }} />);
  expect(screen.getByText('Есть интерес отделов: Отдел ИТ — 85 %')).toBeDefined();
  expect(screen.getByText('5 % · не интересен')).toBeDefined();
  expect(screen.getByText('85 % · интересен')).toBeDefined();
  expect(screen.getByText('Цитаты')).toBeDefined();
});
