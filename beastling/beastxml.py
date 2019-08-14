import datetime
import itertools
import sys
import collections
from io import BytesIO, StringIO
from pathlib import Path

from beastling import __version__
import beastling.beast_maps as beast_maps
from beastling.util import xml


def indent(elem, level=0):
    i = "\n" + level*"  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
        for elem in elem:
            indent(elem, level+1)
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i


def collect_ids_and_refs(root):
    data = dict(id=collections.Counter(), idref=collections.Counter())
    parent_map = {c: p for p in root.iter() for c in p}
    for e in root.iter():
        for attrib, value in e.items():
            for attr, collection in data.items():
                if attrib == attr:
                    if e in parent_map and parent_map[e].tag == 'plate':
                        # Quick and dirty plate handling.
                        # We only support plate matching in direct children of the plate.
                        var = parent_map[e].get('var')
                        for id_ in parent_map[e].get('range').split(','):
                            collection.update([value.replace('$({0})'.format(var), id_)])
                    else:
                        collection.update([value])
            if (attrib not in data) and value.startswith("@"):
                data['idref'].update([value[1:]])
    return data


class BeastXml(object):

    def __init__(self, config, validate=True):
        self.beast = None
        self.beastling_comment = None
        self.config = config
        if not self.config.processed:
            self.config.process()
        self._binary_userdatatype_created = False
        self._covarion_userdatatype_created = False
        # Tell everybody about ourselves
        for model in self.config.all_models:
            model.beastxml = self
        for clock in self.config.clocks:
            clock.beastxml = self
        self._taxon_sets = {}
        self.build_xml()
        if validate:
            self.validate_ids()

    def build_xml(self):
        """
        Creates a complete BEAST XML configuration file as an ElementTree,
        descending from the self.beast element.
        """
        self.beast = xml.beast(
            version="2.0",
            beautitemplate="Standard",
            beautistatus="",
            namespace=':'.join([
                "beast.core",
                "beast.evolution.alignment",
                "beast.evolution.tree.coalescent",
                "beast.core.util",
                "beast.evolution.nuc",
                "beast.evolution.operators",
                "beast.evolution.sitemodel",
                "beast.evolution.substitutionmodel",
                "beast.evolution.likelihood"]),
        )
        self.add_taxon_set(self.beast, "taxa", self.config.languages.languages, define_taxa=True)
        self.add_beastling_comment()
        self.embed_data()
        self.add_maps()
        for model in self.config.models:
            model.add_master_data(self.beast)
            model.add_misc(self.beast)
        for clock in self.config.clocks:
            clock.add_branchrate_model(self.beast)
        self.add_run()
        self.validate_ids()

    def add_beastling_comment(self):
        """
        Add a comment at the root level of the XML document indicating the
        BEASTling version used to create the file, the time and date of
        generation and the original configuration file text.
        """
        comment_lines = []
        comment_lines.append("Generated by BEASTling %s on %s.\n" % (__version__,datetime.datetime.now().strftime("%A, %d %b %Y %I:%M %p")))
        if self.config.cfg.sections():
            comment_lines.append("Original config file:\n")
            buf = StringIO()
            self.config.cfg.write(buf)
            comment_lines.append(buf.getvalue())
            comment_lines.append("Please DO NOT manually edit this file without removing this message or editing")
            comment_lines.append("it to describe the changes made.  Otherwise attempts to replicate your")
            comment_lines.append("analysis using BEASTling and the above configuration may not be valid.\n")
        else:
            comment_lines.append("Configuration built programmatically.")
            comment_lines.append("No config file to include.")
        self.beastling_comment = xml.comment("\n".join(comment_lines))
        self.beast.append(self.beastling_comment)

    def embed_data(self):
        """
        Embed a copy of each data file in a comment at the top of the XML
        document.
        """
        if self.config.admin.embed_data:
            for filename in self.config.files_to_embed:
                self.beast.append(self.format_data_file(filename))
            for model in self.config.models:
                self.beast.append(self.format_data_file(model.data_filename))

    def format_data_file(self, filename):
        """
        Return an ElementTree node corresponding to a comment containing
        the text of the specified data file.
        """
        header = "BEASTling embedded data file: %s" % filename
        return xml.comment("\n".join([header, Path(filename).read_text(encoding='utf8')]))

    def add_maps(self):
        """
        Add <map> elements aliasing common BEAST classes.
        """
        for a, b in beast_maps.maps:
            xml.map(self.beast, text=b, name=a)

    def add_run(self):
        """
        Add the <run> element and all its descendants, which is most of the
        analysis.
        """
        if self.config.mcmc.path_sampling:
            self.add_path_sampling_run()
        else:
            self.add_standard_sampling_run()
        self.estimate_tree_height()
        self.add_state()
        self.add_init()
        self.add_distributions()
        self.add_operators()
        self.add_loggers()

    def add_standard_sampling_run(self):
        """
        Add the <run> element (only) for a standard analysis, i.e. without
        path sampling.  The <state>, <init> etc. are added to whatever this
        method names self.run.
        """
        self.run = xml.run(
            self.beast,
            id="mcmc",
            spec="MCMC",
            chainLength=self.config.mcmc.chainlength,
            numInitializationAttempts=1000,
            sampleFromPrior=self.config.mcmc.sample_from_prior,
        )

    def add_path_sampling_run(self):
        """
        Add the <run> element (only) for a path sampling analysis.  We call
        this self.ps_run and assign the nested <mcmc> element to self.run,
        so that <state>, <init> etc. will be correctly added there.
        """
        attribs = {
            "id": "ps",
            "spec": "beast.inference.PathSampler",
            "chainLength": self.config.mcmc.chainlength,
            "nrOfSteps": self.config.mcmc.steps,
            "alpha": self.config.mcmc.alpha,
            "rootdir": self.config.admin.basename + "_path_sampling",
            "preBurnin": int((self.config.mcmc.preburnin / 100) * self.config.mcmc.chainlength),
            "burnInPercentage": self.config.mcmc.log_burnin,
            "deleteOldLogs": "true",
            }
        if self.config.mcmc.do_not_run:
            attribs["doNotRun"] = "true"
        self.ps_run = xml.run(self.beast, attrib=attribs)
        self.ps_run.text = """cd $(dir)
java -cp $(java.class.path) beast.app.beastapp.BeastMain $(resume/overwrite) -java -seed $(seed) beast.xml"""

        attribs = {}
        attribs["id"] = "mcmc"
        attribs["spec"] = "MCMC"
        attribs["chainLength"] = str(self.config.mcmc.chainlength)
        self.run = xml.mcmc(self.ps_run, attrib=attribs)

    def add_state(self):
        """
        Add the <state> element and all its descendants.
        """
        self.state = xml.state(self.run, id="state", storeEvery="5000")
        self.config.treeprior.add_state_nodes(self)
        for clock in self.config.clocks:
            clock.add_state(self.state)
        for model in self.config.all_models:
            model.add_state(self.state)

    def add_init(self):
        """
        Add the <init> element and all its descendants.
        """
        self.config.treeprior.add_init(self)

    def estimate_tree_height(self):
        """
        Make a rough estimate of what the starting height of the tree should
        be so we can initialise somewhere decent.
        """
        self.config.treeprior.estimate_height(self)

    def add_distributions(self):
        """
        Add all probability distributions under the <run> element.
        """
        self.posterior = xml.distribution(
            self.run, id="posterior", spec="util.CompoundDistribution")
        self.add_prior()
        self.add_likelihood()

    def add_prior(self):
        """
        Add all prior distribution elements.
        """
        self.prior = xml.distribution(
            self.posterior, id="prior", spec="util.CompoundDistribution")
        self.add_monophyly_constraints()
        self.add_calibrations()
        self.config.treeprior.add_prior(self)
        for clock in self.config.clocks:
            clock.add_prior(self.prior)
        for model in self.config.all_models:
            model.add_prior(self.prior)

    def add_monophyly_constraints(self):
        """
        Add monophyly constraints to prior distribution.
        """
        if self.config.languages.monophyly:
            attribs = {}
            attribs["id"] = "constraints"
            attribs["spec"] = "beast.math.distributions.MultiMonophyleticConstraint"
            attribs["tree"] = "@{:}".format(self.config.treeprior.tree_id)
            attribs["newick"] = self.config.languages.monophyly_newick
            xml.distribution(self.prior, attrib=attribs)

    def add_calibrations(self):
        """
        Add timing calibrations to prior distribution.
        """
        # This itertools.cchain is a bit ugly, I wonder if we can get away with sticking them all in one list...
        for clade, cal in sorted(itertools.chain(self.config.calibrations.items(), self.config.tip_calibrations.items())):
            # Don't add an MRCA cal for point calibrations, those only exist to
            # cause the initial tip height to be set
            if cal.dist == "point":
                continue
            # BEAST's logcombiner chokes on spaces...
            clade = clade.replace(" ","_")
            # Create MRCAPrior node
            attribs = {}
            attribs["id"] = clade + "MRCA"
            attribs["monophyletic"] = "true"
            attribs["spec"] = "beast.math.distributions.MRCAPrior"
            attribs["tree"] = "@{:}".format(self.config.treeprior.tree_id)
            if cal.originate:
                attribs["useOriginate"] = "true"
            elif len(cal.langs) == 1:   # If there's only 1 lang and it's not an originate cal, it must be a tip cal
                attribs["tipsonly"] = "true"

            cal_prior = xml.distribution(self.prior, attrib=attribs)

            # Create "taxonset" param for MRCAPrior
            taxonsetname = clade[:-len("_originate")] if clade.endswith("_originate") else clade
            self.add_taxon_set(cal_prior, taxonsetname, cal.langs)

            cal.generate_xml_element(cal_prior)

    def add_taxon_set(self, parent, label, langs, define_taxa=False):
        """
        Add a TaxonSet element with the specified set of languages.

        If a TaxonSet previously defined by this method contains exactly the
        same set of taxa, a reference to that TaxonSet will be added instead.
        By default, each TaxonSet will contain references to the taxa,
        assuming that they have been defined previously (most probably in the
        definition of the tree).  If this is not the case, passing
        define_taxa=True will define, rather than refer to, the taxa.
        """
        # Kill duplicates
        langs = sorted(list(set(langs)))

        # If we've been asked to build an emtpy TaxonSet, something is very wrong,
        # so better to die loud and early
        assert(langs)
        # Refer to any previous TaxonSet with the same languages
        for idref, taxa in self._taxon_sets.items():
            if langs == taxa:
                xml.taxonset(parent, idref=idref)
                return
        if len(langs) == 1 and label == langs[0]:
            # Single taxa are IDs already. They cannot also be taxon set ids.
            label = "tx_{:}".format(label)
        # Otherwise, create and register a new TaxonSet
        taxonset = xml.taxonset(parent, id=label, spec="TaxonSet")
        ## If the taxonset is more than 3 languages in size, use plate notation to minimise XML filesize
        if len(langs) > 3:
            plate = xml.plate(taxonset, var="language", range=langs)
            xml.taxon(plate, attrib={"id" if define_taxa else "idref" :"$(language)"})
        ## Otherwise go for the more readable notation...
        else:
            for lang in langs:
                xml.taxon(taxonset, attrib={"id" if define_taxa else "idref" : lang})
        self._taxon_sets[label] = langs

    def add_likelihood(self):
        """
        Add all likelihood distribution elements.
        """
        self.likelihood = xml.distribution(
            self.posterior, id="likelihood", spec="util.CompoundDistribution")
        for model in self.config.all_models:
            model.add_likelihood(self.likelihood)

    def add_operators(self):
        """
        Add all <operator> elements.
        """
        self.add_tree_operators()
        for clock in self.config.clocks:
            clock.add_operators(self.run)
        for model in self.config.all_models:
            model.add_operators(self.run)
        # Add one DeltaExchangeOperator for feature rates per clock
        for clock in self.config.clocks:
            clock_models = [m for m in self.config.models if m.rate_variation and m.clock == clock]
            if not clock_models:
                continue
            # Add one big DeltaExchangeOperator which operates on all
            # feature clock rates from all models
            delta = xml.operator(
                self.run,
                id="featureClockRateDeltaExchanger:%s" % clock.name,
                spec="DeltaExchangeOperator",
                weight="3.0")
            for model in clock_models:
                plate = xml.plate(delta, var="rate", range=model.all_rates)
                xml.parameter(plate, idref="featureClockRate:%s:$(rate)" % model.name)
            # Add weight vector if there has been any binarisation
            if any([w != 1 for w in itertools.chain(*[m.weights for m in clock_models])]):
                xml.weightvector(
                    delta,
                    text=" ".join(itertools.chain(*[map(str, m.weights) for m in clock_models])),
                    id="featureClockRateWeightParameter:%s" % clock.name,
                    spec="parameter.IntegerParameter",
                    dimension=str(sum([len(m.weights) for m in clock_models])),
                    estimate="false")


    def add_tree_operators(self):
        self.config.treeprior.add_operators(self)

    def add_loggers(self):
        """
        Add all <logger> elements.
        """
        self.add_screen_logger()
        self.add_tracer_logger()
        self.add_tree_loggers()

        # Log individual reconstructed traits (and possibly other per-generation metadata)
        if any([model.metadata for model in self.config.models]):
            self.add_trait_logger("_reconstructed")

    def add_screen_logger(self):
        """
        Add the screen logger, if configured to do so.
        """
        if self.config.admin.screenlog:
            screen_logger = xml.logger(self.run, id="screenlog", logEvery=self.config.admin.log_every)
            xml.log(screen_logger, arg="@posterior", id="ESS.0", spec="util.ESS")
            xml.log(screen_logger, idref="prior")
            xml.log(screen_logger, idref="likelihood")
            xml.log(screen_logger, idref="posterior")

    def add_tracer_logger(self):
        """
        Add file logger, if configured to do so.
        """
        if not (self.config.admin.log_probabilities or self.config.admin.log_params):
            return
        tracer_logger = xml.logger(
            self.run,
            id="tracelog",
            fileName=self.config.admin.path(".log"),
            logEvery=self.config.admin.log_every,
            sort="smart")
        # Log prior, likelihood and posterior
        if self.config.admin.log_probabilities:
            xml.log(tracer_logger, idref="prior")
            xml.log(tracer_logger, idref="likelihood")
            xml.log(tracer_logger, idref="posterior")
        # Log Yule birth rate
        if self.config.admin.log_params:
            self.config.treeprior.add_logging(self, tracer_logger)
            for clock in self.config.clocks:
                clock.add_param_logs(tracer_logger)
            for model in self.config.all_models:
                model.add_param_logs(tracer_logger)

        # Log calibration clade heights
        for clade, cal in sorted(itertools.chain(self.config.calibrations.items(), self.config.tip_calibrations.items())):
            # Don't log unchanging tip heights
            if cal.dist == "point":
                continue
            clade = clade.replace(" ","_")
            xml.log(tracer_logger, idref="%sMRCA" % clade)

    def add_tree_loggers(self):
        """
        Add tree logger, if configured to do so.
        """
        if not self.config.admin.log_trees or self.config.tree_logging_pointless:
            return

        pure_tree_done = False
        non_strict_clocks = set([m.clock for m in self.config.models if not m.clock.is_strict])
        if not non_strict_clocks:
            # All clocks are strict, so we just do one pure log file
            self.add_tree_logger()
            pure_tree_done = True
        else:
            # There are non-strict clocks, so we do one log file each with branch rates
            for clock in non_strict_clocks:
                if len(non_strict_clocks) == 1:
                    self.add_tree_logger("", clock.branchrate_model_id)
                else:
                    self.add_tree_logger("_%s_rates" % clock.name, clock.branchrate_model_id)

        # If asked, do a topology-only tree log (i.e. no branch rates)
        if self.config.admin.log_pure_tree and not pure_tree_done:
            self.add_tree_logger("_pure")

        # Log reconstructed traits (and possibly other per-node metadata)
        if any([model.treedata for model in self.config.models]):
            self.add_trait_tree_logger("_reconstructed")

        # Created a dedicated geographic tree log if asked to log locations,
        # or if the geo model's clock is non-strict
        if not self.config.geo_config:
            return
        if self.config.geo_config["log_locations"] or not self.config.geo_model.clock.is_strict:
            self.add_tree_logger("_geography", self.config.geo_model.clock.branchrate_model_id, True)

    def add_tree_logger(self, suffix="", branchrate_model_id=None, locations=False):
        tree_logger = xml.logger(
            self.run,
            mode="tree",
            fileName=self.config.admin.path(suffix + ".nex"),
            logEvery=self.config.admin.log_every,
            id="treeLogger" + suffix)
        log = xml.log(
            tree_logger,
            id="TreeLoggerWithMetaData" + suffix,
            spec="beast.evolution.tree.TreeWithMetaDataLogger",
            tree="@{:}".format(self.config.treeprior.tree_id),
            dp=self.config.admin.log_dp)
        if branchrate_model_id:
            xml.branchratemodel(log, idref=branchrate_model_id)
        if locations:
            xml.metadata(
                log,
                text="0.0",
                id="location",
                spec="sphericalGeo.TraitFunction",
                likelihood="@sphericalGeographyLikelihood")

    def add_trait_tree_logger(self, suffix=""):
        tree_logger = xml.logger(
            self.run,
            mode="tree",
            fileName=self.config.admin.path(suffix + ".nex"),
            logEvery=self.config.admin.log_every,
            id="treeLogger" + suffix)
        log = xml.log(
            tree_logger,
            id="ReconstructedStateTreeLogger",
            spec="beast.evolution.tree.TreeWithTraitLogger",
            tree="@{:}".format(self.config.treeprior.tree_id))
        for model in self.config.models:
            for md in model.treedata:
                xml.metadata(log, idref=md)

    def add_trait_logger(self, suffix=""):
        """Add a logger referencing all AncestralStateLogger likelihoods in the tree."""
        trait_logger = xml.logger(
            self.run,
            fileName=self.config.admin.path(suffix + ".log"),
            logEvery=self.config.admin.log_every,
            id="traitLogger" + suffix)
        for model in self.config.models:
            for reference in model.metadata:
                xml.log(trait_logger, idref=reference)

    def validate_ids(self):
        data = collect_ids_and_refs(self.beast)
        duplicate_ids = {id_ for id_, count in data['id'].most_common() if count > 1}
        if duplicate_ids:
            raise ValueError("Duplicate BEASTObject IDs found: " + ", ".join(sorted(duplicate_ids)))

        bad_refs = set(data['idref']) - set(data['id'])
        if bad_refs:
            raise ValueError("References to missing BEASTObject IDs found: " + ", ".join(bad_refs))

    def tostring(self):
        """
        Return a string representation of the entire XML document.
        """
        out = BytesIO()
        self.write(out)
        out.seek(0)
        return out.read()

    def write(self, stream):
        indent(self.beast)
        tree = xml.ElementTree(self.beast)
        tree.write(stream, encoding='UTF-8', xml_declaration=True)

    def write_file(self, filename=None):
        """
        Write the XML document to a file.
        """
        if filename in ("stdout", "-"):
            # See https://docs.python.org/3/library/sys.html#sys.stdout
            self.write(getattr(sys.stdout, 'buffer', sys.stdout))
        else:
            filename = Path(filename) if filename else self.config.admin.path(".xml")
            with filename.open("wb") as stream:
                self.write(stream)
