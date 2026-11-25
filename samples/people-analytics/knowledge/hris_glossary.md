# HRIS glossary

## Worker
A worker is anyone with a record in the HRIS: employees and contractors. WORKER_TYP_CD tells them apart. MGR_WORKER_ID is the manager's WORKER_ID.

## Hire dates
ORIG_HIRE_DT is the first day the person ever worked here. LAST_HIRE_DT is the start of the current employment and only differs for rehires.

## FLSA status
FLSA_STAT is EXEMPT or NON_EXEMPT. Non-exempt jobs are eligible for overtime, so overtime_eligible = (FLSA_STAT = 'NON_EXEMPT').

## Recruiting
Time to fill is calculated as FILLED_DT minus OPEN_DT. The HRIS does not store it.
