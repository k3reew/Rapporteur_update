import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, test, vi } from 'vitest';

import { SummaryReview, type SummaryView } from './SummaryReview';
import { CardEditor, CardView, cleanCard, type Card } from './TalkCard';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const card: Card = {
  title: 'Кэширование',
  speakers: [{ name: 'Иванов И. И.', role: 'speaker', affiliation: 'ИСП РАН', contacts: ['ivanov@example.org'] }],
  organizations: [],
  contacts: [],
};

function Editor({ onCard }: { onCard: (card: Card) => void }) {
  const [value, setValue] = useState(card);
  return <CardEditor card={value} onChange={next => { setValue(next); onCard(next); }} />;
}

test('редактор карточки правит авторов и списки через точку с запятой', () => {
  const changes: Card[] = [];
  render(<Editor onCard={value => changes.push(value)} />);

  const others = screen.getByLabelText('Другие организации через «;»');
  fireEvent.change(others, { target: { value: 'МГТУ; ' } });
  // Точка с запятой не пропадает при наборе следующего значения.
  expect((others as HTMLInputElement).value).toBe('МГТУ; ');
  fireEvent.change(others, { target: { value: 'МГТУ; Лаборатория «Пример»' } });
  expect(changes.at(-1)?.organizations).toEqual(['МГТУ', 'Лаборатория «Пример»']);

  fireEvent.click(screen.getByRole('button', { name: 'Добавить автора' }));
  const second = screen.getByRole('group', { name: 'Автор 2' });
  fireEvent.change(within(second).getByLabelText('ФИО'), { target: { value: ' Петров П. П. ' } });
  fireEvent.change(within(second).getByLabelText('Роль'), { target: { value: 'coauthor' } });
  fireEvent.change(within(second).getByLabelText('Контакты через «;»'), { target: { value: '+7 495 000-00-00' } });
  const cleaned = cleanCard(changes.at(-1)!);
  expect(cleaned.speakers[1]).toEqual({ name: 'Петров П. П.', role: 'coauthor', affiliation: null, contacts: ['+7 495 000-00-00'] });

  fireEvent.click(screen.getByRole('button', { name: 'Удалить автора 1' }));
  expect(changes.at(-1)?.speakers.map(speaker => speaker.name)).toEqual([' Петров П. П. ']);
});

test('карточка показывает авторов, организации и контакты', () => {
  render(<CardView card={{ ...card, organizations: ['МГТУ'], contacts: ['www.example.org'] }} />);
  const authors = screen.getByRole('list', { name: 'Авторы' });
  expect(within(authors).getByText('Иванов И. И.')).toBeDefined();
  expect(within(authors).getByText('ivanov@example.org')).toBeDefined();
  expect(screen.getByText('Другие организации:')).toBeDefined();
  expect(screen.getByText('Другие контакты:')).toBeDefined();
});

test('проверка сводки сохраняет карточку целиком', async () => {
  const view: SummaryView = {
    talk_title: 'Кэширование',
    talk_status: 'processed',
    pack: { fragments: [] },
    summary: {
      version: 1, status: 'ready', profile: 'local', human_edit: false, created_at: '2026-11-06T10:00:00Z',
      sentences: [], conclusions: [], claims: [],
    },
  };
  const saved: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/v1/conferences/c/progress') return Response.json({ talks: [] });
    if (url === '/api/v1/talks/talk/summary') return Response.json(view);
    if (url === '/api/v1/talks/talk' && init?.method === 'PUT') {
      saved.push(JSON.parse(String(init.body)));
      return new Response(null, { status: 204 });
    }
    if (url === '/api/v1/talks/talk') return Response.json({ ...card, revision: 4 });
    throw new Error('unexpected ' + url);
  }));
  render(<SummaryReview talkId="talk" conferenceId="c" onClose={() => {}} />);

  fireEvent.click(await screen.findByRole('button', { name: 'Править карточку' }));
  fireEvent.change(screen.getByLabelText('Организация'), { target: { value: 'МГТУ им. Н. Э. Баумана' } });
  fireEvent.change(screen.getByLabelText('Контакты без привязки к автору через «;»'), { target: { value: 'www.example.org' } });
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить карточку' }));

  await screen.findByRole('button', { name: 'Править карточку' });
  expect(saved).toEqual([{
    title: 'Кэширование',
    speakers: [{ name: 'Иванов И. И.', role: 'speaker', affiliation: 'МГТУ им. Н. Э. Баумана', contacts: ['ivanov@example.org'] }],
    organizations: [],
    contacts: ['www.example.org'],
    revision: 4,
  }]);
});
