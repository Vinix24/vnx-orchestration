#!/usr/bin/env tsx
/**
 * Fixture completeness gate.
 *
 * Parses __tests__/*.test.tsx using the TypeScript Compiler API and asserts that
 * every object literal explicitly typed as a target type contains all required
 * (non-optional) fields from lib/types.ts.
 *
 * Catches field additions to DispatchSummary / KanbanCard that aren't reflected
 * in test fixtures before they reach CI.
 *
 * Exit 0 = pass, exit 1 = failures found.
 */

import * as ts from 'typescript';
import * as path from 'path';
import * as fs from 'fs';

export const TARGET_TYPES = ['DispatchSummary', 'KanbanCard'];

export interface FixtureFailure {
  file: string;
  line: number;
  typeName: string;
  missingFields: string[];
}

/** Extract required (non-optional) field names from interface declarations in a .ts file. */
export function extractRequiredFields(
  typesFilePath: string,
  targetTypes: string[],
): Map<string, string[]> {
  const content = fs.readFileSync(typesFilePath, 'utf-8');
  const sourceFile = ts.createSourceFile(
    typesFilePath,
    content,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );

  const result = new Map<string, string[]>();

  ts.forEachChild(sourceFile, (node) => {
    if (ts.isInterfaceDeclaration(node) && targetTypes.includes(node.name.text)) {
      const required: string[] = [];
      node.members.forEach((member) => {
        if (
          ts.isPropertySignature(member) &&
          !member.questionToken &&
          ts.isIdentifier(member.name)
        ) {
          required.push(member.name.text);
        }
      });
      result.set(node.name.text, required);
    }
  });

  return result;
}

function getObjectKeys(obj: ts.ObjectLiteralExpression): string[] {
  const keys: string[] = [];
  for (const prop of obj.properties) {
    if (ts.isPropertyAssignment(prop) && ts.isIdentifier(prop.name)) {
      keys.push(prop.name.text);
    } else if (ts.isShorthandPropertyAssignment(prop)) {
      keys.push(prop.name.text);
    }
  }
  return keys;
}

function hasSpread(obj: ts.ObjectLiteralExpression): boolean {
  return obj.properties.some((p) => ts.isSpreadAssignment(p));
}

function getTypeRefName(typeNode: ts.TypeNode | undefined): string | null {
  if (!typeNode) return null;
  if (ts.isTypeReferenceNode(typeNode) && ts.isIdentifier(typeNode.typeName)) {
    return typeNode.typeName.text;
  }
  return null;
}

/**
 * Scan a single file's content for typed fixture objects and report missing fields.
 *
 * Object literals with spread elements are skipped — the spread may supply
 * any absent field and we can't resolve it statically here.
 */
export function validateFile(
  filePath: string,
  content: string,
  requiredFields: Map<string, string[]>,
): FixtureFailure[] {
  const failures: FixtureFailure[] = [];
  const sourceFile = ts.createSourceFile(
    filePath,
    content,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );

  function getLine(pos: number): number {
    return sourceFile.getLineAndCharacterOfPosition(pos).line + 1;
  }

  function checkObject(
    obj: ts.ObjectLiteralExpression,
    typeName: string,
    nodeStart: number,
  ): void {
    if (hasSpread(obj)) return;
    const keys = getObjectKeys(obj);
    const required = requiredFields.get(typeName);
    if (!required) return;
    const missing = required.filter((f) => !keys.includes(f));
    if (missing.length > 0) {
      failures.push({ file: filePath, line: getLine(nodeStart), typeName, missingFields: missing });
    }
  }

  function visit(node: ts.Node): void {
    // Case 1: const X: TargetType = { ... }
    if (ts.isVariableDeclaration(node)) {
      const typeName = getTypeRefName(node.type);
      if (
        typeName &&
        requiredFields.has(typeName) &&
        node.initializer &&
        ts.isObjectLiteralExpression(node.initializer)
      ) {
        checkObject(node.initializer, typeName, node.getStart(sourceFile));
      }
    }

    // Case 2: expr as TargetType
    if (ts.isAsExpression(node)) {
      const typeName = getTypeRefName(node.type);
      if (
        typeName &&
        requiredFields.has(typeName) &&
        ts.isObjectLiteralExpression(node.expression)
      ) {
        checkObject(node.expression, typeName, node.expression.getStart(sourceFile));
      }
    }

    // Case 3: function f(): TargetType { return { ... } }
    if (
      (ts.isFunctionDeclaration(node) ||
        ts.isFunctionExpression(node) ||
        ts.isArrowFunction(node)) &&
      node.type
    ) {
      const tn = getTypeRefName(node.type);
      if (tn && requiredFields.has(tn) && node.body) {
        const scanReturns = (bodyNode: ts.Node): void => {
          if (
            ts.isFunctionDeclaration(bodyNode) ||
            ts.isFunctionExpression(bodyNode) ||
            ts.isArrowFunction(bodyNode)
          ) {
            return; // don't descend into nested functions
          }
          if (
            ts.isReturnStatement(bodyNode) &&
            bodyNode.expression &&
            ts.isObjectLiteralExpression(bodyNode.expression)
          ) {
            checkObject(bodyNode.expression, tn, bodyNode.expression.getStart(sourceFile));
          }
          ts.forEachChild(bodyNode, scanReturns);
        };
        ts.forEachChild(node.body, scanReturns);
      }
    }

    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return failures;
}

/** Run validation across all test files in the given directory. */
export function runValidation(testsDir: string, typesFilePath: string): FixtureFailure[] {
  const requiredFields = extractRequiredFields(typesFilePath, TARGET_TYPES);

  const testFiles = fs
    .readdirSync(testsDir)
    .filter((f) => f.endsWith('.test.tsx') || f.endsWith('.test.ts'))
    .map((f) => path.join(testsDir, f));

  const allFailures: FixtureFailure[] = [];
  for (const filePath of testFiles) {
    const content = fs.readFileSync(filePath, 'utf-8');
    allFailures.push(...validateFile(filePath, content, requiredFields));
  }

  return allFailures;
}

function main(): void {
  const projectRoot = path.resolve(path.dirname(process.argv[1] ?? __dirname), '..');
  const testsDir = path.join(projectRoot, '__tests__');
  const typesFilePath = path.join(projectRoot, 'lib', 'types.ts');

  if (!fs.existsSync(testsDir)) {
    console.error(`✗ Tests directory not found: ${testsDir}`);
    process.exit(1);
  }

  const failures = runValidation(testsDir, typesFilePath);

  if (failures.length === 0) {
    console.log('✓ All test fixtures contain required fields.');
    process.exit(0);
  }

  console.error(`✗ Fixture completeness failures (${failures.length}):`);
  for (const f of failures) {
    console.error(`  ${f.file}:${f.line} — ${f.typeName} missing: ${f.missingFields.join(', ')}`);
  }
  process.exit(1);
}

const isCli = (process.argv[1] ?? '').endsWith('validate_test_fixtures.ts');
if (isCli) {
  main();
}
