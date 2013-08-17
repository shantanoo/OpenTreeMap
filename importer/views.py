import csv
import json
from StringIO import StringIO
from datetime import datetime

from django.db import transaction
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpResponseRedirect
from django.conf import settings

from django.contrib.auth.decorators import login_required
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.contrib.auth.models import User

import fields

from importer.tasks import run_import_event_validation,\
    commit_import_event

from treemap.models import Species, Neighborhood, Plot,\
    Tree, ExclusionMask, ImportEvent

from importer.models import TreeImportEvent, TreeImportRow,\
    SpeciesImportEvent, SpeciesImportRow, \
    GenericImportEvent, GenericImportRow

from importer import errors

def lowerkeys(h):
    h2 = {}
    for (k,v) in h.iteritems():
        k = k.lower().strip()
        if k != 'ignore':
            h2[k] = v.strip()

    return h2

def find_similar_species(request):
    target = request.REQUEST['target']

    species = Species.objects\
                     .extra(
                         select={
                             'l': "levenshtein(genus || ' ' || species || ' ' || cultivar_name || ' ' || other_part_of_name, %s)"
                         },
                         select_params=(target,))\
                     .order_by('l')[0:2] # Take top 2

    output = [{fields.trees.GENUS: s.genus,
               fields.trees.SPECIES: s.species,
               fields.trees.CULTIVAR: s.cultivar_name,
               fields.trees.OTHER_PART_OF_NAME: s.other_part_of_name,
               'pk': s.pk} for s in species]

    return HttpResponse(
        json.dumps(output),
        content_type = 'application/json')

def counts(request):
    active_trees = TreeImportEvent\
        .objects\
        .order_by('id')\
        .exclude(status=GenericImportEvent.FINISHED_CREATING)\
        .exclude(status=GenericImportEvent.FINISHED_VERIFICATION)\
        .exclude(status=GenericImportEvent.FAILED_FILE_VERIFICATION)

    active_species = SpeciesImportEvent\
        .objects\
        .order_by('id')\
        .exclude(status=GenericImportEvent.FINISHED_CREATING)\
        .exclude(status=GenericImportEvent.FINISHED_VERIFICATION)\
        .exclude(status=GenericImportEvent.FAILED_FILE_VERIFICATION)

    output = {}
    output['trees'] = {t.pk: t.row_type_counts() for t in active_trees}
    output['species'] = {s.pk: s.row_type_counts() for s in active_species }

    return HttpResponse(
        json.dumps(output),
        content_type = 'application/json')

@login_required
def create(request):
    if request.REQUEST['type'] == 'tree':
        typ = 'tree'
        kwargs = {
            'fileconstructor': TreeImportEvent,

            'plot_length_conversion_factor':
            float(request.REQUEST.get('unit_plot_length', 1.0)),

            'plot_width_conversion_factor':
            float(request.REQUEST.get('unit_plot_width', 1.0)),

            'diameter_conversion_factor':
            float(request.REQUEST.get('unit_diameterh', 1.0)),

            'tree_height_conversion_factor':
            float(request.REQUEST.get('unit_tree_height', 1.0)),

            'canopy_height_conversion_factor':
            float(request.REQUEST.get('unit_canopy_height', 1.0))
        }
    elif request.REQUEST['type'] == 'species':
        typ = 'species'
        kwargs = {
            'fileconstructor': SpeciesImportEvent
        }

    pk = process_csv(request,**kwargs)

    return HttpResponseRedirect(reverse('importer.views.list_imports'))

@login_required
def list_imports(request):
    trees = TreeImportEvent.objects\
                           .order_by('id')

    active_trees = trees.exclude(
        status=GenericImportEvent.FINISHED_CREATING)

    finished_trees = trees.filter(
        status=GenericImportEvent.FINISHED_CREATING)

    species = SpeciesImportEvent.objects\
                                .order_by('id')

    active_species = species.exclude(
        status=GenericImportEvent.FINISHED_CREATING)

    finished_species = species.filter(
        status=GenericImportEvent.FINISHED_CREATING)

    return render_to_response(
        'importer/list.html',
        RequestContext(
            request,
            {'trees_active': active_trees,
             'trees_finished': finished_trees,
             'species_active': active_species,
             'species_finished': finished_species }))

@login_required
def show_species_import_status(request, import_event_id):
    return show_import_status(request, import_event_id, SpeciesImportEvent)

@login_required
def show_tree_import_status(request, import_event_id):
    return show_import_status(request, import_event_id, TreeImportEvent)

@login_required
def show_import_status(request, import_event_id, Model):
    return render_to_response(
        'importer/status.html',
        RequestContext(
            request,
            {'event': Model.objects.get(pk=import_event_id)}))

@login_required
def update(request, import_type, import_event_id):
    if import_type == 'tree':
        Model = TreeImportEvent
    else:
        Model = SpeciesImportEvent

    rowdata = json.loads(request.REQUEST['row'])
    idx = rowdata['id']

    row = Model.objects.get(pk=import_event_id).rows().get(idx=idx)
    basedata = row.datadict

    for k,v in rowdata.iteritems():
        if k in basedata:
            basedata[k] = v

    row.datadict = basedata
    row.save()
    row.validate_row()

    return HttpResponse()


# TODO: Remove this method
@login_required
def update_row(request, import_event_row_id):
    update_keys = { key.split('update__')[1]
                    for key
                    in request.REQUEST.keys()
                    if key.startswith('update__') }

    row = TreeImportRow.objects.get(pk=import_event_row_id)

    basedata = row.datadict

    for key in update_keys:
        basedata[key] = request.REQUEST['update__%s' % key]

    row.datadict = basedata
    row.save()
    row.validate_row()

    return HttpResponseRedirect(reverse('importer.views.show_import_status',
                                        args=(row.import_event.pk,)))

@login_required
def results(request, import_event_id, import_type, subtype):
    """ Return a json array for each row of a given subtype
    where subtype is a valid status for a TreeImportRow
    """
    if import_type == 'tree':
        status_map = {
            'success': TreeImportRow.SUCCESS,
            'error': TreeImportRow.ERROR,
            'waiting': TreeImportRow.WAITING,
            'watch': TreeImportRow.WATCH,
            'verified': TreeImportRow.VERIFIED
        }

        Model = TreeImportEvent
    else:
        status_map = {
            'success': SpeciesImportRow.SUCCESS,
            'error': SpeciesImportRow.ERROR,
            'verified': SpeciesImportRow.VERIFIED,
        }
        Model = SpeciesImportEvent

    page_size = 10
    page = int(request.REQUEST.get('page', 0))
    page_start = page_size * page
    page_end = page_size * (page + 1)

    ie = Model.objects.get(pk=import_event_id)

    header = None
    output = {}

    if subtype == 'mergereq':
        query = ie.rows()\
                  .filter(merged=False)\
                  .exclude(status=SpeciesImportRow.ERROR)\
                  .order_by('idx')
    else:
        query = ie.rows()\
                  .filter(status=status_map[subtype])\
                  .order_by('idx')

    if import_type == 'species' and subtype == 'verified':
        query = query.filter(merged=True)

    count = query.count()
    total_pages = int(float(count) / page_size + 1)

    output['total_pages'] = total_pages
    output['count'] = count
    output['rows'] = []

    header_keys = None
    for row in query[page_start:page_end]:
        if header is None:
            header_keys = row.datadict.keys()

        data = {
            'row': row.idx,
            'errors': row.errors_as_array(),
            'data': [row.datadict[k] for k in header_keys]
        }


        # Generate diffs for merge requests
        if subtype == 'mergereq':
            # If errors.TOO_MANY_SPECIES we need to mine species
            # otherwise we can just do simple diff
            ecodes = dict([(e['code'],e['data']) for e in row.errors_as_array()])
            if errors.TOO_MANY_SPECIES[0] in ecodes:
                data['diffs'] = ecodes[errors.TOO_MANY_SPECIES[0]]
            elif errors.MERGE_REQ[0] in ecodes:
                data['diffs'] = [ecodes[errors.MERGE_REQ[0]]]

        if hasattr(row,'plot') and row.plot:
            data['plot_id'] = row.plot.pk

        if hasattr(row,'species') and row.species:
            data['species_id'] = row.species.pk

        output['rows'].append(data)

    output['field_order'] = [f.lower() for f \
                             in json.loads(ie.field_order)
                             if f != "ignore"]
    output['fields'] = header_keys or \
                       ie.rows()[0].datadict.keys()

    return HttpResponse(
        json.dumps(output),
        content_type = 'application/json')

def process_status(request, import_id, TheImportEvent):
    ie = TheImportEvent.objects.get(pk=import_id)

    resp = None
    if ie.errors:
        resp = {'status': 'file_error',
                'errors': json.loads(ie.errors)}
    else:
        errors = []
        for row in ie.rows():
            if row.errors:
                errors.append((row.idx, json.loads(row.errors)))

        if len(errors) > 0:
            resp = {'status': 'row_error',
                    'errors': dict(errors)}

    if resp is None:
        resp = {'status': 'success',
                'rows': ie.rows().count()}

    return HttpResponse(
        json.dumps(resp),
        content_type = 'application/json')

def solve(request, import_event_id, import_row_idx):
    ie = SpeciesImportEvent.objects.get(pk=import_event_id)
    row = ie.rows().get(idx=import_row_idx)

    data = dict(json.loads(request.REQUEST['data']))
    tgtspecies = request.REQUEST['species'];

    # Strip off merge errors
    merge_errors = { errors.TOO_MANY_SPECIES[0],
                     errors.MERGE_REQ[0] }


    ierrors = [e for e in row.errors_as_array()
               if e['code'] not in merge_errors];

    #TODO: Json handling is terrible.
    row.errors = json.dumps(ierrors)
    row.datadict = data

    if tgtspecies != 'new':
        row.species = Species.objects.get(pk=tgtspecies)

    row.merged = True
    row.save()

    rslt = row.validate_row()

    return HttpResponse(
        json.dumps({'status': 'ok',
                    'validates': rslt}),
        content_type = 'application/json')

@transaction.commit_manually
@login_required
def commit(request, import_event_id, import_type=None):
    #TODO:!!! NEED TO ADD TREES TO WATCH LIST
    #TODO:!!! Trees in the same import event should not cause
    #         proximity issues
    #TODO:!!! NEED TO INDICATE TREES TO BE ADDED TO WATCH LIST
    #TODO:!!! NEED TO CLEAR TILE CACHE
    #TODO:!!! If 'Plot' already exists on row *update* when changed
    if import_type == 'species':
        model = SpeciesImportEvent
    elif import_type == 'tree':
        model = TreeImportEvent
    else:
        raise Exception('invalid import type')

    ie = model.objects.get(pk=import_event_id)
    ie.status = GenericImportEvent.CREATING

    ie.save()
    ie.rows().update(status=GenericImportRow.WAITING)

    transaction.commit()

    commit_import_event.delay(ie)
    #TODO: Update tree counts for species

    return HttpResponse(
        json.dumps({'status': 'done'}),
        content_type = 'application/json')

@transaction.commit_manually
def process_csv(request, fileconstructor, **kwargs):
    files = request.FILES
    filename = files.keys()[0]
    fileobj = files[filename]

    owner = request.user
    ie = fileconstructor(file_name=filename,
                         owner=owner,
                         **kwargs)

    # If this is a tree import event it also needs an
    # 'old style' import event
    bie = ImportEvent(file_name=filename)
    bie.save()

    if hasattr(ie, 'base_import_event_id'):
        ie.base_import_event = bie

    ie.save()

    try:
        rows = create_rows_for_event(ie, fileobj)

        transaction.commit()

        if rows:
            run_import_event_validation.delay(ie)

    except Exception, e:
        ie.append_error(errors.GENERIC_ERROR, [], data=str(e))
        ie.status = GenericImportEvent.FAILED_FILE_VERIFICATION
        ie.save()

    return ie.pk


all_species_fields = (
    fields.species.GENUS,
    fields.species.SPECIES,
    fields.species.CULTIVAR,
    fields.species.OTHER_PART_OF_NAME,
    fields.species.COMMON_NAME,
    fields.species.USDA_SYMBOL,
    fields.species.ALT_SYMBOL,
    fields.species.ITREE_CODE,
    fields.species.FAMILY,
    fields.species.NATIVE_STATUS,
    fields.species.FALL_COLORS,
    fields.species.EDIBLE,
    fields.species.FLOWERING,
    fields.species.FLOWERING_PERIOD,
    fields.species.FRUIT_PERIOD,
    fields.species.WILDLIFE,
    fields.species.MAX_DIAMETER,
    fields.species.MAX_HEIGHT,
    fields.species.FACT_SHEET,
)

@login_required
def export_all_species(request):
    io = StringIO()

    # Maps [attr on species model] -> field name
    fieldmap = SpeciesImportRow.SPECIES_MAP

    writer = csv.DictWriter(io, all_species_fields)
    writer.writeheader()

    for s in Species.objects.all():
        obj = {v: getattr(s, k) for (k, v) in fieldmap.iteritems()}
        writer.writerow(obj)

    response = HttpResponse(io.getvalue(), mimetype='text/csv')
    response['Content-Disposition'] = 'attachment; filename=species.csv'

    return response

@login_required
def export_single_species_import(request, import_event_id):
    fieldmap = SpeciesImportRow.SPECIES_MAP
    fields = fieldmap.values()

    ie = SpeciesImportEvent.objects.get(pk=import_event_id)

    io = StringIO()

    writer = csv.DictWriter(io, all_species_fields)
    writer.writeheader()

    for r in ie.rows():
        if r.species:
            obj = {v: getattr(r.species, k) for (k, v) in fieldmap.iteritems()}
        else:
            obj = lowerkeys(json.loads(r.data))

        writer.writerow(obj)

    response = HttpResponse(io.getvalue(), mimetype='text/csv')
    response['Content-Disposition'] = 'attachment; filename=species.csv'

    return response

@login_required
def export_single_tree_import(request, import_event_id):
    plotmap = TreeImportRow.PLOT_MAP
    treemap = TreeImportRow.TREE_MAP

    all_fields = (
        fields.trees.POINT_X,
        fields.trees.POINT_Y,
        fields.trees.ADDRESS,
        fields.trees.PLOT_WIDTH,
        fields.trees.PLOT_LENGTH,
        fields.trees.PLOT_TYPE,
        fields.trees.POWERLINE_CONFLICT,
        fields.trees.SIDEWALK,
        fields.trees.READ_ONLY,
        fields.trees.OPENTREEMAP_ID_NUMBER,
        fields.trees.TREE_PRESENT,
        fields.trees.GENUS,
        fields.trees.SPECIES,
        fields.trees.CULTIVAR,
        fields.trees.OTHER_PART_OF_NAME,
        fields.trees.DIAMETER,
        fields.trees.TREE_HEIGHT,
        fields.trees.ORIG_ID_NUMBER,
        fields.trees.CANOPY_HEIGHT,
        fields.trees.DATE_PLANTED,
        fields.trees.TREE_CONDITION,
        fields.trees.CANOPY_CONDITION,
        fields.trees.ACTIONS,
        fields.trees.PESTS,
        fields.trees.URL,
        fields.trees.NOTES,
        fields.trees.OWNER,
        fields.trees.SPONSOR,
        fields.trees.STEWARD,
        fields.trees.LOCAL_PROJECTS,
        fields.trees.DATA_SOURCE)


    ie = TreeImportEvent.objects.get(pk=import_event_id)

    io = StringIO()

    writer = csv.DictWriter(io, all_fields)
    writer.writeheader()

    for r in ie.rows():
        if r.plot:
            obj = {}
            obj[fields.trees.POINT_X] = r.plot.geometry.x
            obj[fields.trees.POINT_Y] = r.plot.geometry.y

            obj[fields.trees.ADDRESS] = r.plot.address_street
            obj[fields.trees.PLOT_WIDTH] = r.plot.width
            obj[fields.trees.PLOT_LENGTH] = r.plot.length
            obj[fields.trees.READ_ONLY] = r.plot.readonly
            obj[fields.trees.OPENTREEMAP_ID_NUMBER] = r.plot.pk
            obj[fields.trees.ORIG_ID_NUMBER] = r.plot.owner_orig_id
            obj[fields.trees.DATA_SOURCE] = r.plot.owner_additional_id
            obj[fields.trees.NOTES] = r.plot.owner_additional_properties
            obj[fields.trees.SIDEWALK] = r.plot.sidewalk_damage
            obj[fields.trees.POWERLINE_CONFLICT] = r.plot.powerline_conflict_potential
            obj[fields.trees.PLOT_TYPE] = r.plot.type

            tree = r.plot.current_tree()

            obj[fields.trees.TREE_PRESENT] = tree is not None

            if tree:
                species = tree.species

                if species:
                    obj[fields.trees.GENUS] = species.genus
                    obj[fields.trees.SPECIES] = species.species
                    obj[fields.trees.CULTIVAR] = species.cultivar_name
                    obj[fields.trees.OTHER_PART_OF_NAME] = species.other_part_of_name


                obj[fields.trees.DIAMETER] = tree.dbh
                obj[fields.trees.TREE_HEIGHT] = tree.height
                obj[fields.trees.CANOPY_HEIGHT] = tree.canopy_height
                obj[fields.trees.DATE_PLANTED] = tree.date_planted
                obj[fields.trees.OWNER] = tree.tree_owner
                obj[fields.trees.SPONSOR] = tree.sponsor
                obj[fields.trees.STEWARD] = tree.steward_name
                obj[fields.trees.URL] = tree.url

                obj[fields.trees.TREE_CONDITION] = tree.condition
                obj[fields.trees.CANOPY_CONDITION] = tree.canopy_condition
                obj[fields.trees.PESTS] = tree.pests
                obj[fields.trees.LOCAL_PROJECTS] = tree.projects

        else:
            obj = lowerkeys(json.loads(r.data))

        writer.writerow(obj)

    response = HttpResponse(io.getvalue(), mimetype='text/csv')
    response['Content-Disposition'] = 'attachment; filename=trees.csv'

    return response



def process_commit(request, import_id):
    ie = TreeImportEvent.objects.get(pk=import_id)

    rslt = commit_import_event(ie)

    # TODO: What to return here?
    return HttpResponse(
        json.dumps({'status': 'success'}),
        content_type = 'application/json')


def create_rows_for_event(importevent, csvfile):
    rows = []
    reader = csv.DictReader(csvfile)

    fieldnames = reader.fieldnames
    importevent.field_order = json.dumps(fieldnames)
    importevent.save()

    idx = 0
    for row in reader:
        rows.append(
            importevent.create_row(
                data=json.dumps(lowerkeys(row)),
                import_event=importevent, idx=idx))

        # First row
        if idx == 0:
            # Break out early if there was an error
            # with the basic file structure
            importevent.validate_main_file()
            if importevent.has_errors():
                return False

        idx += 1

    return rows
