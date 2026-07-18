import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/** Merge conditional class values with Tailwind conflict resolution. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** Return the URI only when it parses as http or https, so stored data never links other schemes. */
export function webHref(uri: string): string | null {
  try {
    const url = new URL(uri);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

export type WithElementRef<T, U extends HTMLElement = HTMLElement> = T & {
  ref?: U | null;
};

export type WithoutChildren<T> = Omit<T, 'children'>;
export type WithoutChildrenOrChild<T> = Omit<T, 'children' | 'child'>;
export type WithoutChild<T> = Omit<T, 'child'>;
