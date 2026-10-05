// Length formatting, matching backend/services/measure/units.py so a live preview and the
// saved value read the same.

const MM_PER_IN = 25.4;

export function formatImperial(inches: number, denominator = 16): string {
  if (inches < 0) return '-' + formatImperial(-inches, denominator);
  const sixteenths = Math.round(inches * denominator);
  const feet = Math.floor(sixteenths / (12 * denominator));
  let rest = sixteenths - feet * 12 * denominator;
  const whole = Math.floor(rest / denominator);
  rest -= whole * denominator;
  let frac = '';
  if (rest) {
    const g = gcd(rest, denominator);
    frac = `${rest / g}/${denominator / g}`;
  }
  const inch = rest === 0 ? `${whole}` : whole ? `${whole} ${frac}` : frac;
  return feet ? `${feet}'-${inch}"` : `${inch}"`;
}

function gcd(a: number, b: number): number {
  return b ? gcd(b, a % b) : a;
}

export function formatMetric(inches: number): string {
  const mm = inches * MM_PER_IN;
  if (Math.abs(mm) >= 10000) return `${(mm / 1000).toFixed(2)} m`;
  if (Math.abs(mm) >= 1000) return `${(mm / 1000).toFixed(3)} m`;
  return `${mm.toFixed(0)} mm`;
}

export function formatArea(sqIn: number, system: 'imperial' | 'metric'): string {
  const sf = sqIn / 144;
  const sm = sqIn * (MM_PER_IN / 1000) ** 2;
  return system === 'imperial' ? `${sf.toLocaleString('en-US', { maximumFractionDigits: 1, minimumFractionDigits: 1 })} sf`
    : `${sm.toLocaleString('en-US', { maximumFractionDigits: 2, minimumFractionDigits: 2 })} m²`;
}

export const formatLength = (inches: number, system: 'imperial' | 'metric') =>
  system === 'imperial' ? formatImperial(inches) : formatMetric(inches);
