from django.db.models import Count
from django.conf import settings

from django.contrib.gis.db import models
from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

from treemap.models import Plot, Resource
from importer import fields
from importer import errors

from datetime import datetime

from treemap.models import Species, Neighborhood, Plot,\
    Tree, ExclusionMask, ImportEvent

import json

class GenericImportEvent(models.Model):

    class Meta:
        abstract = True

    PENDING_VERIFICATION = 1
    VERIFIYING = 2
    FINISHED_VERIFICATION = 3
    CREATING = 4
    FINISHED_CREATING = 5
    FAILED_FILE_VERIFICATION = 6

    # Original Name of the file
    file_name = models.CharField(max_length=255)

    # Global errors and notices (json)
    errors = models.TextField(default='')

    field_order = models.TextField(default='')

    # Metadata about this particular import
    owner = models.ForeignKey(User)
    created = models.DateTimeField(auto_now=True)
    completed = models.DateTimeField(null=True,blank=True)

    status = models.IntegerField(default=PENDING_VERIFICATION)

    # When false, this dataset is in 'preview' mode
    # When true this dataset has been written to the
    # database
    commited = models.BooleanField(default=False)

    def status_summary(self):
        if self.status == GenericImportEvent.PENDING_VERIFICATION:
            return "Not Yet Started"
        elif self.status == GenericImportEvent.VERIFIYING:
            return "Verifying"
        elif self.status == GenericImportEvent.FINISHED_VERIFICATION:
            return "Verification Complete"
        elif self.status == GenericImportEvent.CREATING:
            return "Creating Trees"
        elif self.status == GenericImportEvent.FAILED_FILE_VERIFICATION:
            return "Invalid File Structure"
        else:
            return "Finished"

    def active(self):
        return self.status != GenericImportEvent.FINISHED_CREATING

    def row_type_counts(self):
        q = self.row_set()\
                .values('status')\
                .annotate(Count('status'))

        return { r['status']: r['status__count'] for r in q }

    def update_status(self):
        """ Update the status field based on current row statuses """
        pass

    def append_error(self, err, data=None):
        code, msg, fatal = err

        if self.errors is None or self.errors == '':
            self.errors = '[]'

        self.errors = json.dumps(
            self.errors_as_array()+ [
                {'code': code,
                 'msg': msg,
                 'data': data,
                 'fatal': fatal}])

        return self

    def errors_as_array(self):
        if self.errors is None or self.errors == '':
            return []
        else:
            return json.loads(self.errors)

    def has_errors(self):
        return len(self.errors_as_array()) > 0

    def row_set(self):
        raise Exception('Abstract Method')

    def rows(self):
        return self.row_set().order_by('idx').all()

    def validate_main_file(self):
        raise Exception('Abstract Method')

class SpeciesImportEvent(GenericImportEvent):
    """
    A TreeImportEvent represents an attempt to upload a csv containing
    species information
    """

    max_diameter_conversion_factor = models.FloatField(default=1.0)
    max_tree_height_conversion_factor = models.FloatField(default=1.0)

    def create_row(self, *args, **kwargs):
        return SpeciesImportRow.objects.create(*args, **kwargs)

    def row_set(self):
        return self.speciesimportrow_set

    def __unicode__(self):
        return u"Species Import #%s" % self.pk

    def status_summary(self):
        if self.status == GenericImportEvent.CREATING:
            return "Creating Species Records"
        else:
            return super(SpeciesImportEvent, self).status_summary()

    def validate_main_file(self):
        """
        Make sure the imported file has rows and valid columns
        """
        if self.rows().count() == 0:
            self.append_error(errors.EMPTY_FILE)

            # This is a fatal error. We need to have at least
            # one row to get header info
            self.status = GenericImportEvent.FAILED_FILE_VERIFICATION
            self.save()
            return False

        has_errors = False
        datastr = self.rows()[0].data
        input_fields = set(json.loads(datastr).keys())

        req = { fields.species.GENUS,
                fields.species.COMMON_NAME, fields.species.ITREE_CODE }

        req -= input_fields
        if req:
            has_errors = True
            self.append_error(errors.MISSING_SPECIES_FIELDS)

        # It is a warning if there are extra input fields
        rem = input_fields - fields.species.ALL
        if len(rem) > 0:
            has_errors = True
            self.append_error(errors.UNMATCHED_FIELDS, list(rem))

        if has_errors:
            self.status = GenericImportEvent.FAILED_FILE_VERIFICATION
            self.save()

        return not has_errors



class TreeImportEvent(GenericImportEvent):
    """
    A TreeImportEvent represents an attempt to upload a csv containing
    tree/plot information
    """

    base_import_event = models.ForeignKey(ImportEvent)

    plot_length_conversion_factor = models.FloatField(default=1.0)
    plot_width_conversion_factor = models.FloatField(default=1.0)
    diameter_conversion_factor = models.FloatField(default=1.0)
    tree_height_conversion_factor = models.FloatField(default=1.0)
    canopy_height_conversion_factor = models.FloatField(default=1.0)

    def create_row(self, *args, **kwargs):
        return TreeImportRow.objects.create(*args, **kwargs)

    def row_set(self):
        return self.treeimportrow_set

    def __unicode__(self):
        return u"Tree Import #%s" % self.pk

    def validate_main_file(self):
        """
        Make sure the imported file has rows and valid columns
        """
        if self.treeimportrow_set.count() == 0:
            self.append_error(errors.EMPTY_FILE)

            # This is a fatal error. We need to have at least
            # one row to get header info
            self.status = GenericImportEvent.FAILED_FILE_VERIFICATION
            self.save()
            return False

        has_errors = False
        datastr = self.treeimportrow_set.all()[0].data
        input_fields = set(json.loads(datastr).keys())

        # Point x/y fields are required
        if (fields.trees.POINT_X not in input_fields or
            fields.trees.POINT_Y not in input_fields):
            has_errors = True
            self.append_error(errors.MISSING_POINTS)

        # It is a warning if there are extra input fields
        rem = input_fields - fields.trees.ALL
        if len(rem) > 0:
            has_errors = True
            self.append_error(errors.UNMATCHED_FIELDS, list(rem))

        if has_errors:
            self.status = GenericImportEvent.FAILED_FILE_VERIFICATION
            self.save()

        return not has_errors




class GenericImportRow(models.Model):
    """
    A row of data and import status
    Subclassed by 'Tree Import Row' and 'Species Import Row'
    """

    class Meta:
        abstract = True

    # JSON dictionary from header <-> rows
    data = models.TextField()

    # Row index from original file
    idx = models.IntegerField()

    finished = models.BooleanField(default=False)

    # JSON field containing error information
    errors = models.TextField(default='')

    # Status
    WAITING=3
    status = models.IntegerField(default=WAITING)

    def __init__(self, *args, **kwargs):
        super(GenericImportRow, self).__init__(*args,**kwargs)
        self.jsondata = None
        self.cleaned = {}

    @property
    def model_fields(self):
        raise Exception('Abstract Method')

    @property
    def datadict(self):
        if self.jsondata is None:
            self.jsondata = json.loads(self.data)

        return self.jsondata

    @datadict.setter
    def datadict(self, v):
        self.jsondata = v
        self.data = json.dumps(self.jsondata)

    def errors_as_array(self):
        if self.errors is None or self.errors == '':
            return []
        else:
            return json.loads(self.errors)

    def has_errors(self):
        return len(self.errors_as_array()) > 0

    def get_fields_with_error(self):
        data = {}
        datadict = self.datadict

        for e in self.errors_as_array():
            for field in e['fields']:
                data[field] = datadict[field]

        return data

    def has_fatal_error(self):
        if self.errors:
            for err in json.loads(self.errors):
                if err['fatal']:
                    return True

        return False


    def append_error(self, err, fields, data=None):
        code, msg, fatal = err

        if self.errors is None or self.errors == '':
            self.errors = '[]'

        # If you give append_error a single field
        # there is no need to get angry
        if isinstance(fields, basestring):
            fields = (fields,) # make into tuple

        self.errors = json.dumps(
            json.loads(self.errors) + [
                {'code': code,
                 'fields': fields,
                 'msg': msg,
                 'data': data,
                 'fatal': fatal}])

        return self

    def safe_float(self, fld):
        try:
            return float(self.datadict[fld])
        except:
            self.append_error(errors.FLOAT_ERROR, fld)
            return False

    def safe_bool(self, fld):
        """ Returns a tuple of (success, bool value) """
        v = self.datadict.get(fld, '').lower()

        if v == '':
            return (True, None)
        if v == 'true' or v == 't' or v == 'yes':
            return (True,True)
        elif v == 'false' or v == 'f' or v == 'no':
            return (True,False)
        else:
            self.append_error(errors.BOOL_ERROR, fld)
            return (False,None)


    def safe_int(self, fld):
        try:
            return int(self.datadict[fld])
        except:
            self.append_error(errors.INT_ERROR, fld)
            return False

    def safe_pos_int(self, fld):
        i = self.safe_int(fld)

        if i is False:
            return False
        elif i < 0:
            self.append_error(errors.POS_INT_ERROR, fld)
            return False
        else:
            return i

    def safe_pos_float(self, fld):
        i = self.safe_float(fld)

        if i is False:
            return False
        elif i < 0:
            self.append_error(errors.POS_FLOAT_ERROR, fld)
            return False
        else:
            return i

    def convert_units(self, data, converts):
        INCHES_TO_DBH_FACTOR = 1.0 / settings.DBH_TO_INCHES_FACTOR

        # Similar to tree
        for fld, factor in converts.iteritems():
            if fld in data and factor != 1.0:
                data[fld] = float(data[fld]) * factor * INCHES_TO_DBH_FACTOR

    def validate_numeric_fields(self):
        def cleanup(fields, fn):
            has_errors = False
            for f in fields:
                if f in self.datadict and self.datadict[f]:
                    maybe_num = fn(f)

                    if maybe_num is False:
                        has_errors = True
                    else:
                        self.cleaned[f] = maybe_num

            return has_errors

        pfloat_ok = cleanup(self.model_fields.POS_FLOAT_FIELDS,
                            self.safe_pos_float)

        float_ok = cleanup(self.model_fields.FLOAT_FIELDS,
                           self.safe_float)

        int_ok = cleanup(self.model_fields.POS_INT_FIELDS,
                         self.safe_pos_int)

        return pfloat_ok and float_ok and int_ok

    def validate_boolean_fields(self):
        has_errors = False
        for f in self.model_fields.BOOLEAN_FIELDS:
            if f in self.datadict:
                success, v = self.safe_bool(f)
                if success and v is not None:
                    self.cleaned[f] = v
                else:
                    has_errors = True

        return has_errors

    def validate_choice_fields(self):
        has_errors = False
        for field,choice_key in self.model_fields.CHOICE_MAP.iteritems():
            value = self.datadict.get(field, None)
            if value:
                all_choices = settings.CHOICES[choice_key]
                choices = { value: id for (id, value) in all_choices }

                if value in choices:
                    self.cleaned[field] = choices[value]
                else:
                    has_errors = True
                    self.append_error(errors.INVALID_CHOICE,
                                      field, choice_key)

        return has_errors

    def validate_string_fields(self):
        has_errors = False
        for field in self.model_fields.STRING_FIELDS:

            value = self.datadict.get(field, None)
            if value:
                if len(value) > 255:
                    self.append_error(errors.STRING_TOO_LONG, field)
                    has_errors = True
                else:
                    self.cleaned[field] = value

        return has_errors

    def validate_date_fields(self):
        has_errors = False
        for field in self.model_fields.DATE_FIELDS:
            value = self.datadict.get(field, None)
            if value:
                try:
                    datep = datetime.strptime(value, '%Y-%m-%d')
                    self.cleaned[self.model_fields.DATE_PLANTED] = datep
                except ValueError, e:
                    self.append_error(errors.INVALID_DATE,
                                      self.model_fields.DATE_PLANTED)
                    has_errors = True

        return has_errors


    def validate_and_convert_datatypes(self):
        self.validate_numeric_fields()
        self.validate_boolean_fields()
        self.validate_choice_fields()
        self.validate_string_fields()
        self.validate_date_fields()

    def validate_row(self):
        """
        Validate a row. Returns True if there were no fatal errors,
        False otherwise

        The method mutates self in two ways:
        - The 'errors' field on self will be appended to
          whenever an error is found
        - The 'cleaned' field on self will be set as fields
          get validated
        """
        raise Exception('Abstract Method')


#TODO: Ok to ignore address?
#TODO: Tree actions (csv field?)

class SpeciesImportRow(GenericImportRow):

    SUCCESS=0
    ERROR=1
    VERIFIED=4

    SPECIES_MAP = {
            'symbol': fields.species.USDA_SYMBOL,
            'alternate_symbol': fields.species.ALT_SYMBOL,
            'itree_code': fields.species.ITREE_CODE,
            'genus': fields.species.GENUS,
            'species': fields.species.SPECIES,
            'cultivar_name': fields.species.CULTIVAR,
            'common_name': fields.species.COMMON_NAME,
            'native_status': fields.species.NATIVE_STATUS,
            'fall_conspicuous': fields.species.FALL_COLORS,
            'palatable_human': fields.species.EDIBLE,
            'flower_conspicuous': fields.species.FLOWERING,
            'bloom_period': fields.species.FLOWERING_PERIOD,
            'fruit_period': fields.species.FRUIT_PERIOD,
            'wildlife_value': fields.species.WILDLIFE,
            'v_max_dbh': fields.species.MAX_DIAMETER,
            'v_max_height': fields.species.MAX_HEIGHT,
            'fact_sheet': fields.species.FACT_SHEET,
            'family': fields.species.FAMILY,
            'other_part_of_name': fields.species.OTHER_PART_OF_NAME
        }

    # Species reference
    species = models.ForeignKey(Species, null=True, blank=True)
    merged = models.BooleanField(default=False)

    import_event = models.ForeignKey(SpeciesImportEvent)

    def diff_from_species(self, species):
        """ Compute how this row is different from
        the given species

        The result is a json dict with field names:
        { '<field name>': ['<species value>', '<row value>'] }

        Note that you can't *remove* data with species import

        If the returned dictionary is empty, importing this
        row will (essentially) be a nop

        This should only be called after a verify because I
        uses cleaned data
        """
        #TODO: Test me
        if species is None:
            return {}

        data = self.cleaned
        rslt = {}
        for (modelkey, rowkey) in SpeciesImportRow.SPECIES_MAP.iteritems():
            rowdata = data.get(rowkey, None)
            modeldata = getattr(species,modelkey)

            if rowdata and rowdata != modeldata:
                    rslt[rowkey] = (modeldata, rowdata)

        # Always include the ID
        rslt['id'] = (species.pk, None)

        return rslt

    @property
    def model_fields(self):
        return fields.species

    def validate_species(self):
        genus = self.datadict.get(fields.species.GENUS,'')
        species = self.datadict.get(fields.species.SPECIES,'')
        cultivar = self.datadict.get(fields.species.CULTIVAR,'')
        other_part = self.datadict.get(fields.species.OTHER_PART_OF_NAME,'')

        # Save these as "empty" strings
        self.cleaned[fields.species.GENUS] = genus
        self.cleaned[fields.species.SPECIES] = species
        self.cleaned[fields.species.CULTIVAR] = cultivar
        self.cleaned[fields.species.OTHER_PART_OF_NAME] = other_part

        if genus != '' or species != '' or cultivar != '' or other_part != '':
            matching_species = Species.objects\
                                      .filter(genus__iexact=genus)\
                                      .filter(species__iexact=species)\
                                      .filter(cultivar_name__iexact=cultivar)\
                                      .filter(other_part_of_name__iexact=other_part)

            self.cleaned[fields.species.ORIG_SPECIES]\
                |= { s.pk for s in matching_species }


        return True

    def validate_code(self, fld, species_fld, addl_filters=None):
        value = self.datadict.get(fld, None)

        if value:
            self.cleaned[fld] = value

            matching_species = Species.objects\
                                      .filter(**{species_fld: value})

            if addl_filters:
                matching_species = matching_species\
                    .filter(**addl_filters)

            self.cleaned[fields.species.ORIG_SPECIES]\
                |= { s.pk for s in matching_species }

        return True

    def validate_usda_code(self):
        # USDA codes don't cover cultivars, so assert that
        # a 'matching' species *must* have the same cultivar
        # and same USDA code
        addl_filter =  {'cultivar_name':
                        self.cleaned.get(fields.species.CULTIVAR,
                                         '')}

        return self.validate_code(fields.species.USDA_SYMBOL,
                                  'symbol', addl_filter)

    def validate_alt_code(self):
        return self.validate_code(fields.species.ALT_SYMBOL,
                                  'alternate_symbol')


    def validate_required_fields(self):
        req = { fields.species.GENUS,
                fields.species.COMMON_NAME, fields.species.ITREE_CODE }

        has_errors = False

        for field in req:
            value = self.cleaned.get(field, None)
            if not value:
                has_errors = True
                self.append_error(errors.MISSING_FIELD, field)

        return not has_errors

    def validate_itree_code(self):
        has_error = False
        itreecode = self.datadict.get(fields.species.ITREE_CODE)
        if itreecode:
            rsrc = Resource.objects.filter(meta_species=itreecode)
            if len(rsrc) == 0:
                has_error = True
                self.append_error(errors.INVALID_ITREE_CODE,
                                  (fields.species.ITREE_CODE,))
            else:
                self.cleaned[fields.species.RESOURCE] = rsrc[0]
        else:
            has_error = True
            self.append_error(errors.MISSING_ITREE_CODE,
                              (fields.species.ITREE_CODE,))

        return not has_error


    def validate_row(self):
        """
        Validate a row. Returns True if there were no fatal errors,
        False otherwise

        The method mutates self in two ways:
        - The 'errors' field on self will be appended to
          whenever an error is found
        - The 'cleaned' field on self will be set as fields
          get validated
        """
        # Clear errrors
        self.errors = ''

        # NOTE: Validations append errors directly to importrow
        # and move data over to the 'cleaned' hash as it is
        # validated

        # Convert all fields to correct datatypes
        self.validate_and_convert_datatypes()

        # Check to see if this species matches any existing ones
        # they'll be stored as a set of ORIG_SPECIES
        self.cleaned[fields.species.ORIG_SPECIES] = set()

        self.validate_species()
        self.validate_usda_code()
        self.validate_alt_code()

        self.validate_itree_code()
        self.validate_required_fields()

        # Native status is a horrible field that pretends to
        # be a boolean value but is actually a string so we
        # change it here
        if fields.species.NATIVE_STATUS in self.cleaned:
            self.cleaned[fields.species.NATIVE_STATUS] = str(
                self.cleaned[fields.species.NATIVE_STATUS])


        # If same is set to true this is essentially a no-op
        same = False

        possible_matches = self.cleaned[fields.species.ORIG_SPECIES]
        # TODO: Certain fields require this flag to be reset
        if not self.merged:
            if len(possible_matches) == 0:
                self.merged = True
            else:
                species = [Species.objects.get(pk=pk) for pk in possible_matches]
                diffs = [self.diff_from_species(s) for s in species]
                # There's always a single field that has changed in the
                # diff. This is the 'id' field of the existing species,
                # which will never be the same as the None for the current
                # id.
                if all([diff.keys() == ['id'] for diff in diffs]):
                    self.merged = True
                    same = True
                else:
                    diff_keys = set()

                    for diff in diffs:
                        for key in diff.keys():
                            diff_keys.add(key)

                    if len(possible_matches) > 1:
                        self.append_error(errors.TOO_MANY_SPECIES, tuple(diff_keys), tuple(diffs))
                    else:
                        self.append_error(errors.MERGE_REQ, tuple(diff_keys), diffs[0])
                        pk = list(possible_matches)[0]
                        self.species = Species.objects.get(pk=pk)

        fatal = False
        if self.has_fatal_error():
            self.status = SpeciesImportRow.ERROR
            fatal = True
        elif same: # Nothing changed, this has been effectively added
            self.status = SpeciesImportRow.SUCCESS
        else:
            self.status = SpeciesImportRow.VERIFIED

        self.save()
        return not fatal

    def commit_row(self):
        # First validate
        if not self.validate_row():
            return False

        # Get our data
        data = self.cleaned

        species_edited = False

        # Initially grab species from row if it exists
        # and edit it
        species = self.species

        # If not specified create a new one
        if species is None:
            species = Species()

        # Convert units
        self.convert_units(data, {
            fields.species.MAX_DIAMETER:
            self.import_event.max_diameter_conversion_factor,

            fields.species.MAX_HEIGHT:
            self.import_event.max_tree_height_conversion_factor
        })

        #TODO: Update tree count nonsense

        for modelkey, importdatakey in SpeciesImportRow.SPECIES_MAP.iteritems():
            importdata = data.get(importdatakey, None)

            if importdata is not None:
                species_edited = True
                setattr(species, modelkey, importdata)

        if species_edited:
            species.save()

        resource = data[fields.species.RESOURCE]

        species.resource.clear()
        species.resource.add(resource)

        species.save()
        resource.save()

        self.species = species
        self.status = TreeImportRow.SUCCESS
        self.save()

        return True

class TreeImportRow(GenericImportRow):
    SUCCESS=0
    ERROR=1
    WATCH=2
    VERIFIED=4

    PLOT_MAP = {
        'geometry': fields.trees.POINT,
        'width': fields.trees.PLOT_WIDTH,
        'length': fields.trees.PLOT_LENGTH,
        'type': fields.trees.PLOT_TYPE,
        'readonly': fields.trees.READ_ONLY,
        'sidewalk_damage': fields.trees.SIDEWALK,
        'powerline_conflict_potential': fields.trees.POWERLINE_CONFLICT,
        'owner_orig_id': fields.trees.ORIG_ID_NUMBER,
        'owner_additional_id': fields.trees.DATA_SOURCE,
        'owner_additional_properties': fields.trees.NOTES
    }

    TREE_MAP = {
        'tree_owner': fields.trees.OWNER,
        'steward_name': fields.trees.STEWARD,
        'dbh': fields.trees.DIAMETER,
        'height': fields.trees.TREE_HEIGHT,
        'canopy_height': fields.trees.CANOPY_HEIGHT,
        'species': fields.trees.SPECIES_OBJECT,
        'sponsor': fields.trees.SPONSOR,
        'date_planted': fields.trees.DATE_PLANTED,
        'readonly': fields.trees.READ_ONLY,
        'projects': fields.trees.LOCAL_PROJECTS,
        'condition': fields.trees.TREE_CONDITION,
        'canopy_condition': fields.trees.CANOPY_CONDITION,
        'url': fields.trees.URL,
        'pests': fields.trees.PESTS
    }

    # plot that was created from this row
    plot = models.ForeignKey(Plot, null=True, blank=True)

    # The main import event
    import_event = models.ForeignKey(TreeImportEvent)

    @property
    def model_fields(self):
        return fields.trees

    def commit_row(self):
        # If this row was already commit... abort
        if self.plot:
            self.status = TreeImportRow.SUCCESS
            self.save()

        # First validate
        if not self.validate_row():
            return False

        # Get our data
        data = self.cleaned

        self.convert_units(data, {
            fields.trees.PLOT_WIDTH:
            self.import_event.plot_width_conversion_factor,

            fields.trees.PLOT_LENGTH:
            self.import_event.plot_length_conversion_factor,

            fields.trees.DIAMETER:
            self.import_event.diameter_conversion_factor,

            fields.trees.TREE_HEIGHT:
            self.import_event.tree_height_conversion_factor,

            fields.trees.CANOPY_HEIGHT:
            self.import_event.canopy_height_conversion_factor
        })

        # We need the import event from treemap.models
        # the names of things are a bit odd here but
        # self.import_event ->
        #   TreeImportEvent (importer) ->
        #     ImportEvent (treemap)
        #
        base_treemap_import_event = self.import_event.base_import_event

        plot_edited = False
        tree_edited = False

        # Initially grab plot from row if it exists
        plot = self.plot
        if plot is None:
            plot = Plot(present=True)

        # Event if TREE_PRESENT is None, a tree
        # can still be spawned here if there is
        # any tree data later
        tree = plot.current_tree()

        # Check for an existing tree:
        if self.model_fields.OPENTREEMAP_ID_NUMBER in data:
            plot = Plot.objects.get(
                pk=data[self.model_fields.OPENTREEMAP_ID_NUMBER])
            tree = plot.current_tree()
        else:
            if data.get(self.model_fields.TREE_PRESENT, False):
                tree_edited = True
                if tree is None:
                    tree = Tree(present=True)

        data_owner = self.import_event.owner

        for modelkey, importdatakey in TreeImportRow.PLOT_MAP.iteritems():
            importdata = data.get(importdatakey, None)

            if importdata:
                plot_edited = True
                setattr(plot, modelkey, importdata)

        if plot_edited:
            plot.last_updated_by = data_owner
            plot.import_event = base_treemap_import_event
            plot.save()

        for modelkey, importdatakey in TreeImportRow.TREE_MAP.iteritems():
            importdata = data.get(importdatakey, None)

            if importdata:
                tree_edited = True
                if tree is None:
                    tree = Tree(present=True)
                setattr(tree, modelkey, importdata)

        if tree_edited:
            tree.last_updated_by = data_owner
            tree.import_event = base_treemap_import_event
            tree.plot = plot
            tree.save()

        self.plot = plot
        self.status = TreeImportRow.SUCCESS
        self.save()

        return True

    def validate_geom(self):
        x = self.cleaned.get(fields.trees.POINT_X, None)
        y = self.cleaned.get(fields.trees.POINT_Y, None)

        # Note, this shouldn't really happen since main
        # file validation will fail, but butter safe than sorry
        if x is None or y is None:
            self.append_error(errors.MISSING_POINTS,
                              (fields.trees.POINT_X, fields.trees.POINT_Y))
            return False

        # Simple validation
        # longitude must be between -180 and 180
        # latitude must be betwen -90 and 90
        if abs(x) > 180 or abs(y) > 90:
            self.append_error(errors.INVALID_GEOM,
                              (fields.trees.POINT_X, fields.trees.POINT_Y))
            return False

        p = Point(x,y)

        if ExclusionMask.objects.filter(geometry__contains=p).exists():
            self.append_error(errors.EXCL_ZONE,
                              (fields.trees.POINT_X, fields.trees.POINT_Y))
            return False
        elif Neighborhood.objects.filter(geometry__contains=p).exists():
            self.cleaned[fields.trees.POINT] = p
        else:
            self.append_error(errors.GEOM_OUT_OF_BOUNDS,
                              (fields.trees.POINT_X, fields.trees.POINT_Y))
            return False

        return True

    def validate_otm_id(self):
        oid = self.cleaned.get(fields.trees.OPENTREEMAP_ID_NUMBER, None)
        if oid:
            has_plot = Plot.objects.filter(
                pk=oid).exists()

            if not has_plot:
                self.append_error(errors.INVALID_OTM_ID,
                                  fields.trees.OPENTREEMAP_ID_NUMBER)
                return False

        return True

    def validate_proximity(self, point):
        base_import_event = self.import_event.base_import_event
        nearby = Plot.objects\
                     .filter(present=True,
                             geometry__distance_lte=(point, D(ft=10.0)))\
                     .distance(point)\
                     .exclude(import_event=base_import_event)\
                     .order_by('distance')[:5]

        if len(nearby) > 0:
            self.append_error(errors.NEARBY_TREES,
                              (fields.trees.POINT_X, fields.trees.POINT_Y),
                              [p.pk for p in nearby])
            return False
        else:
            return True

    def validate_species_max(self, field, max_val, err):
        inputval = self.cleaned.get(field, None)
        if inputval:
            if max_val and inputval > max_val:
                self.append_error(err, field, max_val)
                return False

        return True


    def validate_species_dbh_max(self, species):
        return self.validate_species_max(
            fields.trees.DIAMETER,
            species.v_max_dbh, errors.SPECIES_DBH_TOO_HIGH)

    def validate_species_height_max(self, species):
        return self.validate_species_max(
            fields.trees.TREE_HEIGHT,
            species.v_max_height, errors.SPECIES_HEIGHT_TOO_HIGH)

    def validate_species(self):
        genus = self.datadict.get(fields.trees.GENUS,'')
        species = self.datadict.get(fields.trees.SPECIES,'')
        cultivar = self.datadict.get(fields.trees.CULTIVAR,'')
        other_part = self.datadict.get(fields.trees.OTHER_PART_OF_NAME,'')

        if genus != '' or species != '' or cultivar != '':
            matching_species = Species.objects\
                                      .filter(genus__iexact=genus)\
                                      .filter(species__iexact=species)\
                                      .filter(cultivar_name__iexact=cultivar)\
                                      .filter(other_part_of_name__iexact=other_part)

            if len(matching_species) == 1:
                self.cleaned[fields.trees.SPECIES_OBJECT] = matching_species[0]
            else:
                self.append_error(errors.INVALID_SPECIES,
                                  (fields.trees.GENUS, fields.trees.SPECIES, fields.trees.CULTIVAR),
                                  ' '.join([genus,species,cultivar]).strip())
                return False

        return True

    def validate_row(self):
        """
        Validate a row. Returns True if there were no fatal errors,
        False otherwise

        The method mutates self in two ways:
        - The 'errors' field on self will be appended to
          whenever an error is found
        - The 'cleaned' field on self will be set as fields
          get validated
        """
        # Clear errrors
        self.errors = ''

        # NOTE: Validations append errors directly to importrow
        # and move data over to the 'cleaned' hash as it is
        # validated

        # Convert all fields to correct datatypes
        self.validate_and_convert_datatypes()

        # We can work on the 'cleaned' data from here on out
        self.validate_otm_id()

        # Attaches a GEOS point to fields.trees.POINT
        self.validate_geom()

        # This could be None or not set if there
        # was an earlier error
        pt = self.cleaned.get(fields.trees.POINT, None)

        self.validate_species()

        # This could be None or unset if species data were
        # not given
        species = self.cleaned.get(fields.trees.SPECIES_OBJECT, None)

        # These validations are non-fatal
        if species:
            self.validate_species_dbh_max(species)
            self.validate_species_height_max(species)

        if pt:
            self.validate_proximity(pt)

        fatal = False
        if self.has_fatal_error():
            self.status = TreeImportRow.ERROR
            fatal = True
        elif self.has_errors(): # Has 'warning'/tree watch errors
            self.status = TreeImportRow.WATCH
        else:
            self.status = TreeImportRow.VERIFIED

        self.save()
        return not fatal
