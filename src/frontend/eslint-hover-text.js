// Hover text (`title`) is one short phrase: see AGENTS.md "UI". Checks the
// fixed words of each title, as an attribute, a `title:` property, a
// `.title =` assignment or what a function named `…Title` returns; other
// text from variables or calls is not checked.

const MAX_LENGTH = 60;

// The fixed text of a value: string literals, the static parts of template
// literals, both branches of a condition.
function texts(node) {
  if (node === null || node === undefined) return [];
  switch (node.type) {
    case "Literal":
      return typeof node.value === "string" ? [node.value] : [];
    case "TemplateLiteral":
      return [node.quasis.map((quasi) => quasi.value.cooked ?? "").join("")];
    case "JSXExpressionContainer":
      return texts(node.expression);
    case "ConditionalExpression":
      return [...texts(node.consequent), ...texts(node.alternate)];
    case "LogicalExpression":
      return [...texts(node.left), ...texts(node.right)];
    default:
      return [];
  }
}

function problem(text) {
  if (text.length > MAX_LENGTH)
    return `Hover text is ${text.length} characters; keep it under ${MAX_LENGTH}.`;
  // After a letter: "3. Kraków" is a zone's name, not a sentence.
  if (/(\p{L}\.|;)(\s|$)/u.test(text.trim()))
    return "Hover text is one phrase: no full stops or semicolons.";
  return null;
}

// Reported at the attribute or property, so a disable comment goes above it.
function check(context, node, value) {
  for (const text of texts(value)) {
    const message = problem(text);
    if (message !== null) context.report({ node, message });
  }
}

const isTitleFunction = (node) =>
  /Title$/.test(
    node.id?.name ??
      (node.parent.type === "VariableDeclarator" ? node.parent.id.name : ""),
  );

const isTitle = (key) =>
  key?.type === "Identifier"
    ? key.name === "title"
    : key?.type === "Literal" && key.value === "title";

export const hoverText = {
  meta: {
    type: "suggestion",
    docs: { description: "Keep hover text one short phrase" },
    schema: [],
  },
  create(context) {
    // Whether each function we are inside is a `…Title` one.
    const functions = [];
    return {
      ":function"(node) {
        const title = isTitleFunction(node);
        functions.push(title);
        if (title && node.expression) check(context, node.body, node.body);
      },
      ":function:exit"() {
        functions.pop();
      },
      ReturnStatement(node) {
        if (functions.at(-1)) check(context, node, node.argument);
      },
      JSXAttribute(node) {
        if (node.name.name === "title") check(context, node, node.value);
      },
      Property(node) {
        if (!node.computed && isTitle(node.key))
          check(context, node, node.value);
      },
      AssignmentExpression(node) {
        if (
          node.left.type === "MemberExpression" &&
          !node.left.computed &&
          node.left.property.name === "title"
        )
          check(context, node, node.right);
      },
    };
  },
};
