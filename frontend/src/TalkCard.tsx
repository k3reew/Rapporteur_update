import { useState } from 'react';

/** Карточка доклада: тема, авторы с организацией и контактами, сведения без привязки к автору. */
export type Speaker = { name: string; role?: 'speaker' | 'coauthor'; affiliation?: string | null; position?: string | null; contacts?: string[] };
export type Card = { title: string; speakers: Speaker[]; organizations?: string[]; contacts?: string[] };

const roles: [NonNullable<Speaker['role']>, string][] = [['speaker', 'Докладчик'], ['coauthor', 'Соавтор']];

function parseList(text: string): string[] {
  return text.split(';').map(item => item.trim()).filter(Boolean);
}

function sameList(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((item, index) => item === b[index]);
}

/** Список через «;». Введённый текст хранится, пока он соответствует значению: так точка
 * с запятой и пробел не пропадают при наборе, а внешний сброс значения виден сразу. */
function ListInput({ label, value, onChange }: { label: string; value: string[]; onChange: (value: string[]) => void }) {
  const [text, setText] = useState('');
  const shown = sameList(parseList(text), value) ? text : value.join('; ');
  return <label className="field">{label}<input value={shown} onChange={event => {
    setText(event.target.value);
    onChange(parseList(event.target.value));
  }} /></label>;
}

/** Значения для сервера: без пустых авторов и пробелов по краям. */
export function cleanCard<T extends Card>(card: T): T {
  return {
    ...card,
    title: card.title.trim(),
    speakers: card.speakers
      .filter(speaker => speaker.name.trim())
      .map(speaker => ({
        ...speaker,
        name: speaker.name.trim(),
        affiliation: speaker.affiliation?.trim() || null,
        ...(speaker.position !== undefined ? { position: speaker.position?.trim() || null } : {}),
        contacts: (speaker.contacts ?? []).map(item => item.trim()).filter(Boolean),
      })),
    organizations: (card.organizations ?? []).map(item => item.trim()).filter(Boolean),
    contacts: (card.contacts ?? []).map(item => item.trim()).filter(Boolean),
  };
}

export function CardEditor<T extends Card>({ card, onChange, titleLabel = 'Тема' }: {
  card: T; onChange: (card: T) => void; titleLabel?: string;
}) {
  const change = (changes: Partial<Card>) => onChange({ ...card, ...changes });
  const changeSpeaker = (index: number, changes: Partial<Speaker>) =>
    change({ speakers: card.speakers.map((speaker, i) => i === index ? { ...speaker, ...changes } : speaker) });
  const hasAffiliations = card.speakers.some(speaker => speaker.affiliation);
  return <div className="card-editor">
    <label className="field">{titleLabel}<input value={card.title} onChange={event => change({ title: event.target.value })} /></label>
    <fieldset className="card-authors">
      <legend>Авторы</legend>
      {card.speakers.map((speaker, index) => <div className="card-author" key={index} role="group" aria-label={`Автор ${index + 1}`}>
        <label className="field">ФИО<input value={speaker.name} onChange={event => changeSpeaker(index, { name: event.target.value })} /></label>
        <label className="field">Роль<select value={speaker.role ?? 'speaker'} onChange={event => changeSpeaker(index, { role: event.target.value as Speaker['role'] })}>
          {roles.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <label className="field">Организация<input value={speaker.affiliation ?? ''} onChange={event => changeSpeaker(index, { affiliation: event.target.value })} /></label>
        <label className="field">Должность<input value={speaker.position ?? ''} onChange={event => changeSpeaker(index, { position: event.target.value })} /></label>
        <ListInput label="Контакты через «;»" value={speaker.contacts ?? []} onChange={contacts => changeSpeaker(index, { contacts })} />
        <button type="button" className="link" aria-label={`Удалить автора ${index + 1}`}
          onClick={() => change({ speakers: card.speakers.filter((_, i) => i !== index) })}>Удалить</button>
      </div>)}
      <button type="button" onClick={() => change({ speakers: [...card.speakers, { name: '', role: 'speaker', affiliation: null, contacts: [] }] })}>Добавить автора</button>
    </fieldset>
    <ListInput label={hasAffiliations ? 'Другие организации через «;»' : 'Организации через «;»'}
      value={card.organizations ?? []} onChange={organizations => change({ organizations })} />
    <ListInput label="Контакты без привязки к автору через «;»" value={card.contacts ?? []} onChange={contacts => change({ contacts })} />
  </div>;
}

export function CardView({ card }: { card: Card }) {
  const hasAffiliations = card.speakers.some(speaker => speaker.affiliation);
  const hasContacts = card.speakers.some(speaker => speaker.contacts?.length);
  return <div className="card-view">
    {card.speakers.length ? <ul className="card-people" aria-label="Авторы">
      {card.speakers.map((speaker, index) => <li key={index}>
        <strong>{speaker.name}</strong>{speaker.role === 'coauthor' && ' (соавтор)'}
        {speaker.affiliation && <> — {speaker.affiliation}</>}
        {speaker.position && <small>{speaker.position}</small>}
        {!!speaker.contacts?.length && <small>{speaker.contacts.join(', ')}</small>}
      </li>)}
    </ul> : <p className="notice">Автор не установлен</p>}
    {!!card.organizations?.length && <p><b>{hasAffiliations ? 'Другие организации' : 'Организации'}:</b> {card.organizations.join('; ')}</p>}
    {!!card.contacts?.length && <p><b>{hasContacts ? 'Другие контакты' : 'Контакты'}:</b> {card.contacts.join(', ')}</p>}
  </div>;
}
