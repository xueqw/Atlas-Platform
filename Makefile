ACCEPTANCE_PROJECT ?= atlas-acceptance
ACCEPTANCE = ATLAS_ACCEPTANCE_PROJECT=$(ACCEPTANCE_PROJECT) scripts/acceptance/manage.sh

.PHONY: acceptance-init acceptance-up acceptance-wait acceptance-status acceptance-seed \
	acceptance-backup acceptance-rehearse acceptance-down acceptance-clean

acceptance-init:
	$(ACCEPTANCE) init

acceptance-up:
	$(ACCEPTANCE) up

acceptance-wait:
	$(ACCEPTANCE) wait

acceptance-status:
	$(ACCEPTANCE) status

acceptance-seed:
	$(ACCEPTANCE) seed

acceptance-backup:
	$(ACCEPTANCE) backup

acceptance-rehearse:
	$(ACCEPTANCE) rehearse

acceptance-down:
	$(ACCEPTANCE) down

acceptance-clean:
	$(ACCEPTANCE) clean
