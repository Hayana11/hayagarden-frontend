type ObjectConstructorWithHasOwn = ObjectConstructor & {
  hasOwn?: (object: object, property: PropertyKey) => boolean;
};

export function installObjectHasOwnCompat(): void {
  const objectConstructor = Object as ObjectConstructorWithHasOwn;
  if (typeof objectConstructor.hasOwn === 'function') return;

  const hasOwnProperty = Object.prototype.hasOwnProperty;
  Object.defineProperty(objectConstructor, 'hasOwn', {
    configurable: true,
    writable: true,
    value: (object: object, property: PropertyKey) => hasOwnProperty.call(object, property),
  });
}
