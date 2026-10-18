// jsdom has no canvas: getContext() returns null but also prints a "Not
// implemented" warning on every call. Return the same null quietly.
HTMLCanvasElement.prototype.getContext = () => null;
