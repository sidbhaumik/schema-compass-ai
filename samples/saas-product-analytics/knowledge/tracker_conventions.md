# Tracker naming conventions

## Identifiers
Tracker tables use camelCase. Ids end in `Id` (`userId`, `acctId`), timestamps end in `Ts` and are UTC unless the table notes otherwise, and codes end in `Cd`.

## Properties
`props` is a VARIANT holding event-specific attributes. Load it as a JSON string; analysts flatten the keys they need downstream.

## Platforms
`platformCd` values are upper case (WEB, IOS, ANDROID). The target expects lower case.
